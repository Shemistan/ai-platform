"""Надёжность: лог процесса (REL-009), SIGTERM и сироты (REL-003), таймауты (REL-004),
/api/ready (REL-008), уведомления в Telegram (REL-011), выпуск после рестарта (REL-007)."""

import json
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aiplatform import config as config_module
from aiplatform import server as server_module
from aiplatform.core import gate as gate_module
from aiplatform.core import log, runner
from aiplatform.core import tasks as tasks_module
from aiplatform.core.notify import Notifier
from aiplatform.studio import pipeline, publish
from tests import TMP_ROOT
from tests.helpers import PlatformCase, isolated_config

PLATFORM = Path(__file__).resolve().parents[1]
PY = sys.executable


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(cond, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


def all_done(_ctx):
    return None


def studio_steps(**override):
    return {s.id: override.get(s.id, all_done) for s in pipeline.STEPS}


class LogCase(unittest.TestCase):
    """Лог процесса во временном каталоге; после теста обработчики снимаются."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.log_path = log.setup(self.tmp)
        self.addCleanup(log.close)

    def events(self, name=None):
        for handler in log.logger.handlers:
            handler.flush()
        found = lines(self.log_path) if self.log_path.exists() else []
        return [e for e in found if name is None or e["event"] == name]


# --- REL-009: лог процесса ---


class ProcessLogTest(LogCase):
    def test_step_writes_json_lines_with_duration(self):
        studio = pipeline.Studio(self.tmp / "projects", studio_steps())
        project = studio.create("article", "Тест", {})
        studio.run(project["id"])
        studio.join(project["id"], 10)
        starts = self.events("step.start")
        dones = self.events("step.done")
        self.assertEqual([e["step"] for e in starts], list(pipeline.STEP_IDS))
        self.assertEqual(len(dones), len(pipeline.STEPS))
        self.assertTrue(all(isinstance(e["duration_s"], float) and e["project"] == project["id"] for e in dones))
        self.assertTrue(all({"ts", "level", "event"} <= set(e) for e in dones))

    def test_failed_step_has_error(self):
        def boom(_ctx):
            raise pipeline.StudioError("нет сети")

        studio = pipeline.Studio(self.tmp / "projects", studio_steps(text=boom))
        project = studio.create("article", "Тест", {})
        studio.run(project["id"])
        studio.join(project["id"], 10)
        failed = self.events("step.failed")
        self.assertEqual(failed[0]["step"], "text")
        self.assertEqual(failed[0]["level"], "WARNING")
        self.assertIn("нет сети", failed[0]["error"])

    def test_rotation_by_size(self):
        log.close()
        path = log.setup(self.tmp, max_bytes=2000, backups=2)
        for n in range(200):
            log.event("spam", n=n, text="x" * 50)
        self.assertTrue(path.with_name("platform.log.1").exists())
        self.assertTrue(path.with_name("platform.log.2").exists())
        self.assertFalse(path.with_name("platform.log.3").exists())
        self.assertLess(path.stat().st_size, 2500)

    def test_notifications_log_rotates(self):
        path = self.tmp / "notifications.log"
        path.write_text("x" * 100, encoding="utf-8")
        log.rotate(path, max_bytes=50, backups=2)
        self.assertFalse(path.exists())
        self.assertTrue(path.with_name("notifications.log.1").exists())
        notifier = Notifier(self.tmp, fake=True)
        with mock.patch.object(log, "MAX_BYTES", 10):
            notifier.send("a", "b", show=False)
        self.assertEqual(notifier.recent()[0]["title"], "a")

    def test_task_events(self):
        tasks = tasks_module.Tasks(self.tmp / "tasks")
        self.addCleanup(tasks.close)
        task = tasks.start(tasks_module.Spec(kind="t", title="ok", argv=[PY, "-c", "print('hi')"]))
        tasks.wait(task["id"], 10)
        done = self.events("task.done")
        self.assertEqual(done[0]["task"], task["id"])
        self.assertIn("duration_s", done[0])


class AccessLogTest(PlatformCase):
    extra_env = {"AIP_SCHEDULE_TICK_S": "0.1"}

    def setUp(self):
        super().setUp()
        self.log_path = log.setup(Path(self.tmp.name))
        self.addCleanup(log.close)

    def events(self, name):
        for handler in log.logger.handlers:
            handler.flush()
        return [e for e in lines(self.log_path) if e["event"] == name] if self.log_path.exists() else []

    def test_polls_are_not_logged_but_actions_and_errors_are(self):
        for _ in range(3):
            self.assertEqual(self.call("GET", "/api/status")[0], 200)
        self.call("GET", "/api/nope")
        self.call("POST", "/api/settings/section/cleanup", {"days": 14})
        access = self.events("http.access")
        paths = [(e["method"], e["path"], e["status"]) for e in access]
        self.assertNotIn("/api/status", [p for _, p, _ in paths])
        self.assertIn(("GET", "/api/nope", 404), paths)
        self.assertIn(("POST", "/api/settings/section/cleanup", 200), paths)

    def test_schedule_tick_is_logged(self):
        self.assertTrue(wait_for(lambda: self.events("schedule.tick"), timeout=5))
        self.assertIn("result", self.events("schedule.tick")[0])

    def test_unhandled_error_is_500_and_logged(self):
        with mock.patch.object(self.app, "status", side_effect=RuntimeError("сломалось")):
            status, body = self.json("GET", "/api/status")
        self.assertEqual(status, 500)
        self.assertIn("error", body)
        error = self.events("http.error")[0]
        self.assertIn("RuntimeError: сломалось", error["error"])


# --- REL-003: SIGTERM, гашение процессов, сироты ---


class ShutdownTest(PlatformCase):
    def test_stop_kills_model_processes_and_rejects_posts(self):
        cancel = threading.Event()
        errors = []
        started = []

        def model():
            try:
                runner.run([PY, "-c", "import time; print('go', flush=True); time.sleep(60)"], started.append, cancel)
            except runner.Canceled:
                errors.append("canceled")

        thread = threading.Thread(target=model)
        thread.start()
        self.assertTrue(wait_for(lambda: started))
        pid_files = list((Path(self.tmp.name) / "run").glob("*.json"))
        self.assertEqual(len(pid_files), 1)
        pgid = json.loads(pid_files[0].read_text())["pgid"]

        self.app.stopping = True  # первое, что делает request_stop: новые задачи — 503
        status, body = self.json("POST", "/api/settings/section/cleanup", {"days": 14})
        self.assertEqual(status, 503, body)
        self.assertEqual(self.json("GET", "/api/status")[0], 200, "чтение работает до конца остановки")

        begin = time.monotonic()
        self.app.stopping = False
        server_module.request_stop(self.server, self.app, "SIGTERM")
        thread.join(15)
        self.assertTrue(wait_for(lambda: self.app.closed, timeout=15))
        self.assertLess(time.monotonic() - begin, 15)
        self.assertEqual(errors, ["canceled"], "остановка сервиса — отмена, а не сбой")
        self.assertFalse(group_alive(pgid))
        self.assertEqual(list((Path(self.tmp.name) / "run").glob("*.json")), [])


class OrphanTest(unittest.TestCase):
    def test_orphan_from_previous_run_is_killed(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name) / "run"
        run_dir.mkdir()
        argv = [PY, "-c", "import time; time.sleep(60)"]
        orphan = subprocess.Popen(argv, start_new_session=True)
        self.addCleanup(orphan.wait, 5)
        (run_dir / f"{orphan.pid}.json").write_text(json.dumps({"pgid": orphan.pid, "argv": argv, "started_at": 0}))
        (run_dir / "999999.json").write_text(json.dumps({"pgid": 999999, "argv": ["нет-такой"], "started_at": 0}))
        # ps в песочнице тестов может быть недоступен — команду процесса подставляем.
        command = mock.patch.object(runner, "_command_of", lambda pid: " ".join(argv) if pid == orphan.pid else "")
        with command, self.assertLogs("aiplatform", logging.WARNING) as logs:
            killed = runner.reap_orphans(run_dir)
        self.assertEqual([k["pgid"] for k in killed], [orphan.pid])
        self.assertIn("process.orphan_killed", logs.output[0])
        self.assertIsNotNone(orphan.wait(5))
        self.assertEqual(list(run_dir.iterdir()), [])

    def test_foreign_process_with_same_pid_is_not_touched(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        run_dir = Path(tmp.name)
        other = subprocess.Popen(["sleep", "30"], start_new_session=True)
        self.addCleanup(other.wait, 5)
        self.addCleanup(other.kill)
        (run_dir / f"{other.pid}.json").write_text(
            json.dumps({"pgid": other.pid, "argv": ["/opt/model/bin/generate"], "started_at": 0})
        )
        with mock.patch.object(runner, "_command_of", lambda _pid: "sleep 30"):
            self.assertEqual(runner.reap_orphans(run_dir), [])
        self.assertIsNone(other.poll())


class SigtermProcessTest(unittest.TestCase):
    def test_sigterm_exits_cleanly(self):
        env = isolated_config({**os.environ, "AIP_BACKEND": "fake", "AIP_DATA_DIR": "", "AIP_PORT": "0"})
        env["AIP_SCHEDULE_TICK_S"] = "0"
        proc = subprocess.Popen(
            [PY, "-m", "aiplatform.server"],
            cwd=PLATFORM,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        self.addCleanup(proc.stdout.close)
        try:
            line = proc.stdout.readline()
            while line and "ai-platform: http://" not in line:
                line = proc.stdout.readline()
            self.assertIn("ai-platform: http://", line)
            begin = time.monotonic()
            proc.send_signal(signal.SIGTERM)
            self.assertEqual(proc.wait(15), 0)
            self.assertLess(time.monotonic() - begin, 15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        events = [e["event"] for e in lines(Path(env["AIP_DATA_DIR"]) / "logs" / "platform.log")]
        self.assertIn("process.start", events)
        self.assertIn("process.stopping", events)
        self.assertIn("process.stop", events)


# --- REL-004: таймауты ---


class TimeoutTest(unittest.TestCase):
    def test_total_timeout(self):
        begin = time.monotonic()
        with self.assertRaises(runner.RunFailed) as err:
            runner.run([PY, "-c", "import time; time.sleep(30)"], lambda _l: None, threading.Event(), timeout_s=0.5)
        self.assertLess(time.monotonic() - begin, 10)
        self.assertIn("Превышено", str(err.exception))

    def test_idle_timeout(self):
        script = "import time\nfor _ in range(3):\n    print('tick', flush=True); time.sleep(0.1)\ntime.sleep(30)"
        with self.assertRaises(runner.RunFailed) as err:
            runner.run([PY, "-c", script], lambda _l: None, threading.Event(), idle_timeout_s=1, kind="image")
        self.assertIn("Нет вывода", str(err.exception))
        self.assertIn("генерация картинки", str(err.exception))

    def test_limits_from_settings(self):
        self.assertEqual(runner.limit("llm"), (600, None))
        runner.configure(None, lambda: {"llm": {"total": 2, "idle": 1}})
        self.addCleanup(runner.configure, None)
        self.assertEqual(runner.limit("llm"), (120, 60))
        self.assertEqual(runner.limit(None), (None, None))

    def test_settings_keep_only_valid_timeouts(self):
        from aiplatform.core import settings

        raw = {"timeouts": {"llm": {"total": 5, "idle": -1}, "nope": {"total": 1}, "video": {"total": True}}}
        self.assertEqual(settings.normalize(raw)["timeouts"], {"llm": {"total": 5}})

    def test_task_timeout_frees_gate_for_next(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        gate = gate_module.Gate()
        tasks = tasks_module.Tasks(Path(tmp.name), gate)
        self.addCleanup(tasks.close)
        slow = tasks.start(
            tasks_module.Spec(
                kind="t", title="висит", argv=[PY, "-c", "import time; time.sleep(30)"], gpu="Фото", timeout_s=0.5
            )
        )
        nxt = tasks.start(tasks_module.Spec(kind="t", title="следом", argv=[PY, "-c", "print(1)"], gpu="Видео"))
        self.assertEqual(tasks.wait(slow["id"], 15)["status"], "failed")
        self.assertIn("Превышено", tasks.get(slow["id"])["error"])
        self.assertEqual(tasks.wait(nxt["id"], 15)["status"], "done")
        self.assertIsNone(gate.holder())

    def test_gate_held_since(self):
        gate = gate_module.Gate()
        self.assertIsNone(gate.held())
        with gate.hold("Фото"):
            held = gate.held()
            self.assertEqual(held["owner"], "Фото")
            self.assertGreaterEqual(held["held_s"], 0)
        self.assertIsNone(gate.held())


class StatusGateTest(PlatformCase):
    def test_status_shows_held_since(self):
        release = threading.Event()

        def hold():
            with self.app.ctx.gate.hold("Фото"):
                release.wait(10)

        thread = threading.Thread(target=hold)
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(release.set)
        self.assertTrue(wait_for(lambda: self.app.ctx.gate.holder()))
        status, body = self.json("GET", "/api/status")
        self.assertEqual(body["gpu"], "Фото")
        self.assertIn("gpu_since", body)
        self.assertGreaterEqual(body["gpu_held_s"], 0)


# --- REL-008: /api/ready ---


class ReadyTest(PlatformCase):
    def setUp(self):
        super().setUp()
        self.real_problems = server_module.App.problems
        patcher = mock.patch.object(server_module.App, "problems", lambda _self: [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def ready(self):
        return self.json("GET", "/api/ready")

    def test_ready(self):
        status, body = self.ready()
        self.assertEqual(status, 200, body)
        names = {c["name"] for c in body["checks"]}
        self.assertLessEqual({"problems", "disk", "schedule", "token", "last_digest"}, names)

    def test_disk_full(self):
        usage = mock.Mock(free=1 * 2**30)
        with mock.patch("shutil.disk_usage", return_value=usage):
            status, body = self.ready()
        self.assertEqual(status, 503)
        self.assertEqual(body["failed"], ["disk"])

    def test_models_dir_not_mounted(self):
        missing = Path(self.tmp.name) / "External SSD" / "local_models"
        with mock.patch.object(server_module.App, "models_dir", lambda _self: missing):
            status, body = self.ready()
        self.assertEqual(status, 503)
        self.assertEqual(body["failed"], ["models_dir"])
        with mock.patch.object(server_module.App, "models_dir", lambda _self: missing):
            self.assertTrue(any("не примонтирован" in p for p in self.real_problems(self.app)))

    def test_schedule_stopped(self):
        scheduler = self.app.modules["studio"].scheduler
        scheduler.close()
        status, body = self.ready()
        self.assertEqual(status, 503)
        self.assertEqual(body["failed"], ["schedule"])

    def test_schedule_tick_late(self):
        scheduler = self.app.modules["studio"].scheduler
        scheduler.last_tick_at = time.time() - 3 * scheduler.tick_s
        self.assertFalse(scheduler.alive())
        self.assertEqual(self.ready()[1]["failed"], ["schedule"])

    def test_no_token_for_digest_channel(self):
        channel = publish.Channel(id="voice-news", label="Telegram: VOICE_NEWS", type=publish.TELEGRAM, chat_id="-100")
        studio = self.app.modules["studio"]
        with mock.patch.object(type(studio), "_digest_channels", lambda _self: [channel]), mock.patch.dict(os.environ):
            os.environ.pop(publish.NARRATOR_TOKEN_ENV, None)
            os.environ.pop(publish.NARRATOR_TOKEN_FILE_ENV, None)
            status, body = self.ready()
            self.assertIn("нет токена", "; ".join(studio.problems()))
        self.assertEqual(status, 503)
        self.assertEqual(body["failed"], ["token"])

    def test_last_digest_failed(self):
        scheduler = self.app.modules["studio"].scheduler
        scheduler._mark(
            "digest", {"day": time.strftime("%Y-%m-%d"), "result": "failed", "error": "диск", "attempts": 3}
        )
        status, body = self.ready()
        self.assertEqual(status, 503)
        self.assertEqual(body["failed"], ["last_digest"])


# --- REL-011: Telegram ---


class FakeBot:
    def __init__(self, answer=None):
        self.calls = []
        self.answer = answer or {"ok": True, "result": {"message_id": 1}}

    def __call__(self, url, fields):
        self.calls.append((url, fields))
        return self.answer


class TelegramNotifyTest(LogCase):
    def test_important_goes_to_owner_chat(self):
        bot = FakeBot()
        notifier = Notifier(self.tmp, fake=True, telegram=lambda: ("123:abc", {"chat_id": "42"}), telegram_post=bot)
        results = notifier.send("Выпуск упал", "Дайджест — Озвучка: нет сети", show=True)
        self.assertEqual(results, [{"via": "telegram", "ok": True, "error": None}])
        url, fields = bot.calls[0]
        self.assertTrue(url.endswith("/bot123:abc/sendMessage"))
        self.assertEqual(fields["chat_id"], "42")
        self.assertIn("Выпуск упал", fields["text"])
        self.assertEqual(self.events("notify.sent")[0]["via"], "telegram")

    def test_not_shown_not_sent(self):
        bot = FakeBot()
        notifier = Notifier(self.tmp, fake=True, telegram=lambda: ("t", {"chat_id": "1"}), telegram_post=bot)
        notifier.send("x", "y", show=False)
        self.assertEqual(bot.calls, [])

    def test_failure_is_logged_without_token(self):
        bot = FakeBot({"ok": False, "description": "Forbidden: bot 123:abc was blocked by the user"})
        notifier = Notifier(self.tmp, fake=True, telegram=lambda: ("123:abc", {"chat_id": "42"}), telegram_post=bot)
        result = notifier.send("x", "y", show=True)[0]
        self.assertFalse(result["ok"])
        failed = self.events("notify.failed")[0]
        self.assertIn("blocked", failed["error"])
        self.assertNotIn("123:abc", failed["error"])

    def test_network_error_is_logged(self):
        def down(_url, _fields):
            raise OSError("https://api.telegram.org/bot123:abc/sendMessage")

        notifier = Notifier(self.tmp, fake=True, telegram=lambda: ("123:abc", {"chat_id": "42"}), telegram_post=down)
        self.assertFalse(notifier.send("x", "y", show=True)[0]["ok"])
        self.assertNotIn("123:abc", json.dumps(self.events("notify.failed")))


class TelegramPlatformTest(PlatformCase):
    def setUp(self):
        super().setUp()
        self.bot = FakeBot()
        notifier = self.app.ctx.notifier
        notifier.telegram_post = self.bot
        studio = self.app.modules["studio"]
        notifier.telegram = lambda: ("123:abc", studio.owner_channel().target()) if studio.owner_channel() else None

    def test_chat_enables_notifications_and_failed_step_reaches_telegram(self):
        status, state = self.json("POST", "/api/settings/section/notify", {"telegram_chat": "42"})
        self.assertEqual(status, 200, state)
        self.assertEqual(state["notify"], {"enabled": True, "telegram_chat": "42"})
        project = {"title": "Дайджест", "archived": False, "running": False, "steps": {}}
        project["steps"] = {s.id: {"status": "done"} for s in pipeline.STEPS}
        project["steps"]["voice"] = {"status": "failed", "error": "нет сети"}
        self.app.modules["studio"]._on_stop(project, {"voice": "running", "publish": "done"})
        self.assertEqual(len(self.bot.calls), 1)
        self.assertEqual(self.bot.calls[0][1]["chat_id"], "42")
        self.assertIn("Выпуск упал", self.bot.calls[0][1]["text"])

    def test_bad_chat_rejected(self):
        status, _ = self.json("POST", "/api/settings/section/notify", {"telegram_chat": "чат"})
        self.assertEqual(status, 400)

    def test_check_button_sends_and_reports_failure(self):
        self.json("POST", "/api/settings/section/notify", {"telegram_chat": "42"})
        status, body = self.json("POST", "/api/settings/notify-test")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["notify_test"][0]["via"], "telegram")
        self.assertIn("Проверка уведомлений", self.bot.calls[0][1]["text"])
        self.bot.answer = {"ok": False, "description": "chat not found"}
        status, body = self.json("POST", "/api/settings/notify-test")
        self.assertEqual(status, 502)
        self.assertIn("chat not found", body["error"])


# --- REL-007: выпуск после рестарта ---


class ResumeAfterRestartTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def crashed(self, step_id, *, auto=True, resumed=False):
        """Выпуск, у которого шаг step_id «выполнялся», когда процесс умер."""
        studio = pipeline.Studio(self.root, studio_steps())
        project = studio.create("digest", "Дайджест", {})
        path = self.root / project["id"] / "project.json"
        data = json.loads(path.read_text())
        before = pipeline.STEP_IDS[: pipeline.STEP_IDS.index(step_id)]
        for sid in before:
            data["steps"][sid]["status"] = "done"
        data["steps"][step_id] |= {"status": "running"} | ({"resumed": True} if resumed else {})
        data |= {"running": True, "auto": auto}
        path.write_text(json.dumps(data))
        return project["id"]

    def test_auto_project_resumes_interrupted_step_once(self):
        pid = self.crashed("voice")
        calls = []
        studio = pipeline.Studio(self.root, studio_steps(voice=lambda ctx: calls.append(ctx.step)))
        self.assertEqual(studio.interrupted, [(pid, "voice")])
        self.assertEqual(studio.get(pid)["steps"]["voice"]["error"], pipeline.INTERRUPTED)
        self.assertTrue(studio.resume_interrupted(pid, "voice"))
        studio.join(pid, 10)
        self.assertEqual(calls, ["voice"])
        self.assertEqual(studio.get(pid)["steps"]["voice"]["status"], "done")

    def test_second_restart_does_not_loop(self):
        pid = self.crashed("voice", resumed=True)
        studio = pipeline.Studio(self.root, studio_steps())
        self.assertFalse(studio.resume_interrupted(pid, "voice"))
        self.assertEqual(studio.get(pid)["steps"]["voice"]["status"], "failed")

    def test_publish_and_manual_projects_are_not_resumed(self):
        pid = self.crashed("publish")
        manual = self.crashed("voice", auto=False)
        studio = pipeline.Studio(self.root, studio_steps())
        self.assertFalse(studio.resume_interrupted(pid, "publish"))
        self.assertFalse(studio.resume_interrupted(manual, "voice"))

    def test_shutdown_leaves_step_interrupted(self):
        """SIGTERM посреди шага: после рестарта шаг прерван, а не «остановлен человеком»."""
        entered = threading.Event()

        def slow(ctx):
            entered.set()
            runner.run([PY, "-c", "import time; time.sleep(30)"], lambda _l: None, ctx.cancel)

        studio = pipeline.Studio(self.root, studio_steps(text=slow))
        project = studio.create("digest", "Дайджест", {})
        studio.run(project["id"])
        self.assertTrue(entered.wait(5))
        time.sleep(0.3)
        studio.close()
        self.assertEqual(studio.get(project["id"])["steps"]["text"]["status"], "running")
        again = pipeline.Studio(self.root, studio_steps())
        self.assertEqual(again.interrupted, [(project["id"], "text")])


class RestartNotifyTest(unittest.TestCase):
    def test_notification_and_resume_after_restart(self):
        env = isolated_config({"AIP_BACKEND": "fake", "AIP_PORT": "0", "AIP_SCHEDULE_TICK_S": "0"})
        config = config_module.load(env)
        projects = config.data_dir / "studio" / "projects"
        studio = pipeline.Studio(projects, studio_steps())
        made = studio.create("digest", "Дайджест за вчера", {})
        path = projects / made["id"] / "project.json"
        data = json.loads(path.read_text())
        data["steps"]["text"]["status"] = "running"
        data |= {"running": True, "auto": True}
        path.write_text(json.dumps(data))

        server, app = server_module.make_server(config)
        self.addCleanup(server.server_close)
        self.addCleanup(app.close)
        note = app.ctx.notifier.recent()[0]
        self.assertEqual(note["title"], "Выпуск прерван перезапуском: Дайджест за вчера")
        self.assertIn("продолжаю", note["text"])
        state = app.modules["studio"].studio.get(made["id"])
        self.assertTrue(state["steps"]["text"].get("resumed"))


if __name__ == "__main__":
    unittest.main()
