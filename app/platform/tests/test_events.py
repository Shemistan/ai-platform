"""Журнал событий выпуска (AN-002): запись, последовательность на фейковых шагах, ожидание, перезапуск, бэкфилл."""

import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.core import events, media
from aiplatform.studio import backfill, pipeline
from aiplatform.studio.llm import StudioError
from tests.test_studio_publish_journal import JournalCase


def fake_steps(calls: list[str], fail: set[str] | None = None) -> dict:
    """Шаги-пустышки: публикация ждёт подтверждения, шаги из fail падают."""

    def make(step_id: str):
        def run(ctx: pipeline.StepContext) -> None:
            calls.append(step_id)
            if step_id in (fail or set()):
                raise StudioError("сломалось")
            if step_id == "short":
                raise pipeline.Skip("шортс выключен")
            if step_id == "publish" and not ctx.state.get("approved"):
                raise pipeline.Waiting("Нажми «Опубликовать».")

        return run

    return {s: make(s) for s in pipeline.STEP_IDS}


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.log = events.EventLog(self.dir / events.FILE)
        self.calls: list[str] = []

    def studio(self, **kwargs) -> pipeline.Studio:
        studio = pipeline.Studio(self.dir / "studio" / "projects", fake_steps(self.calls, **kwargs), journal=self.log)
        self.addCleanup(studio.close)
        return studio

    def run_all(self, studio, pid):
        studio.run(pid)
        studio.join(pid, timeout=10)


class EventLogTest(Case):
    def test_schema_and_props(self):
        found = self.log.emit("step.started", project_id="p", step="text", attempt=1, duration_s=1.23456, color="red")
        self.assertEqual(found["actor"], events.SYSTEM)
        self.assertEqual((found["attempt"], found["duration_s"], found["props"]), (1, 1.235, {"color": "red"}))
        self.assertNotIn("error_class", found)  # None не пишется
        self.assertEqual(self.log.read(), [found])

    def test_write_failure_does_not_raise(self):
        broken = events.EventLog(self.dir)  # каталог вместо файла — открыть на запись нельзя
        self.assertIsNone(broken.emit("project.created", project_id="p"))

    def test_actor_by_thread(self):
        import threading

        seen = []
        t = threading.Thread(target=lambda: seen.append(events.actor()), name=events.SCHEDULER_THREAD)
        t.start()
        t.join()
        self.assertEqual((events.actor(), seen), (events.HUMAN, [events.SCHEDULER]))


class PipelineEventsTest(Case):
    def test_release_sequence_wait_and_approve(self):
        studio = self.studio()
        pid = studio.create("article", "Т", {})["id"]
        self.run_all(studio, pid)
        seq = [(e["event"], e.get("step")) for e in self.log.read()]
        self.assertEqual(seq[0], ("project.created", None))
        self.assertEqual(seq[1:3], [("step.started", "text"), ("step.finished", "text")])
        self.assertIn(("step.skipped", "short"), seq)
        self.assertEqual(seq[-2:], [("step.started", "publish"), ("step.waiting", "publish")])
        finished = [e for e in self.log.read() if e["event"] == "step.finished"]
        self.assertTrue(all(e["duration_s"] >= 0 and e["attempt"] == 1 for e in finished))
        self.assertEqual(self.log.read()[0]["actor"], events.HUMAN)
        self.assertEqual({e["actor"] for e in self.log.read()[1:]}, {events.SYSTEM})

        time.sleep(0.05)
        studio.approve(pid, "publish")
        studio.join(pid, timeout=10)
        tail = self.log.read()[len(seq) :]
        approved = tail[0]
        self.assertEqual(
            (approved["event"], approved["actor"], approved["step"]), ("step.approved", "human", "publish")
        )
        self.assertGreaterEqual(approved["duration_s"], 0.05)
        self.assertEqual([e["event"] for e in tail[1:]], ["step.started", "step.finished"])
        self.assertEqual(tail[1]["attempt"], 2)
        self.assertNotIn("waiting_since", studio.get(pid)["steps"]["publish"])

    def test_rerun_counts_attempts(self):
        studio = self.studio()
        pid = studio.create("article", "Т", {})["id"]
        self.run_all(studio, pid)
        before = len(self.log.read())
        studio.run(pid, "text")
        studio.join(pid, timeout=10)
        tail = self.log.read()[before:]
        self.assertEqual([e["event"] for e in tail], ["step.rerun", "step.started", "step.finished"])
        self.assertEqual((tail[0]["attempt"], tail[0]["actor"], tail[1]["attempt"]), (2, "human", 2))

    def test_failure_has_error_class_and_human_upload_closes_wait(self):
        studio = self.studio(fail={"voice"})
        pid = studio.create("article", "Т", {})["id"]
        self.run_all(studio, pid)
        failed = next(e for e in self.log.read() if e["event"] == "step.failed")
        self.assertEqual(
            (failed["step"], failed["error_class"], failed["status_to"]), ("voice", "StudioError", "failed")
        )
        studio.mark_done(pid, "voice", note="Загружено вручную.")
        done = self.log.read()[-1]
        self.assertEqual((done["event"], done["actor"], done["status_from"]), ("step.human_input", "human", "failed"))
        self.assertNotIn("duration_s", done)  # шаг не ждал, а упал: ожидания не было

    def test_journal_failure_does_not_break_pipeline(self):
        studio = pipeline.Studio(self.dir / "p", fake_steps(self.calls), journal=events.EventLog(self.dir))
        self.addCleanup(studio.close)
        pid = studio.create("article", "Т", {})["id"]
        self.run_all(studio, pid)
        self.assertEqual(studio.get(pid)["steps"]["publish"]["status"], "waiting")

    def test_log_stamp_has_date(self):
        studio = self.studio()
        pid = studio.create("article", "Т", {})["id"]
        self.run_all(studio, pid)
        self.assertRegex(studio.get(pid)["log"][0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ▶ Текст$")


class PublicationEventsTest(JournalCase):
    def test_sent_failed_then_retry(self):
        pid = self.video_project()
        self.tg.refuse.add(("tg-b", "sendVideo"))
        self.press(pid, channels=["tg-a", "tg-b"])
        self.tg.refuse.clear()
        self.press(pid, channels=["tg-a", "tg-b"])
        log = events.EventLog(Path(self.tmp.name) / events.FILE).read()
        pubs = [(e["event"], e["channel_id"]) for e in log if e["event"].startswith("publication.")]
        self.assertEqual(
            pubs, [("publication.sent", "tg-a"), ("publication.failed", "tg-b"), ("publication.sent", "tg-b")]
        )
        rerun = next(e for e in log if e["event"] == "step.rerun")
        self.assertEqual((rerun["step"], rerun["attempt"], rerun["actor"]), ("publish", 2, "human"))
        self.assertEqual(next(e for e in log if e["event"] == "project.created")["project_id"], pid)


class BackfillTest(Case):
    def make_project(self, pid="aaaaaaaaaaaa"):
        folder = self.dir / "studio" / "projects" / pid
        folder.mkdir(parents=True)
        created = time.mktime((2026, 9, 1, 23, 59, 0, 0, 0, -1))
        project = {
            "id": pid,
            "scenario": "digest",
            "title": "Д",
            "created_at": created,
            "updated_at": created + 7200,
            "archived": True,
            "steps": {
                "text": {"status": "done", "started_at": created, "updated_at": created + 60},
                "voice": {"status": "done", "started_at": created + 100, "updated_at": created + 160},
                "publish": {"status": "waiting", "updated_at": created + 300},
            },
            "log": [
                "23:59:00 ▶ Текст",
                "00:00:30 ✓ Текст",
                "00:01:00 [Текст] прочее",
                "00:02:00 ▶ Текст",
                "00:03:00 ✗ Текст: x",
            ],
        }
        (folder / "project.json").write_text(json.dumps(project), encoding="utf-8")
        published = [
            {"channel": "tg", "ok": True, "state": "sent", "sent": created + 400, "attempt_id": "x1",
             "stats": [{"at": created + 3600, "views": 10, "members": 100}]},
            {"channel": "download-16x9", "ok": True, "files": ["publish/download-16x9/video.mp4"]},
        ]  # fmt: skip
        (folder / "published.json").write_text(json.dumps(published), encoding="utf-8")
        return created

    def test_fill_dimensions_once(self):
        # AN-006: media_s отправленного файла и формат для записей до него — разово, без повторов.
        self.make_project()
        folder = self.dir / "studio" / "projects" / "aaaaaaaaaaaa"
        (folder / "short.mp4").write_bytes(b"y")
        (folder / "video.mp4").write_bytes(b"x")
        formats = {"tg": "vertical", "download-16x9": "landscape"}
        with mock.patch.object(media, "duration", lambda path, *a: 40.0 if path.name == "short.mp4" else 300.0):
            first = backfill.fill_dimensions(self.dir, formats)
            again = backfill.fill_dimensions(self.dir, formats)
        self.assertEqual((first, again["records"]), ({"projects": 1, "records": 2}, 0))
        records = {r["channel"]: r for r in json.loads((folder / "published.json").read_text(encoding="utf-8"))}
        tg = records["tg"]
        self.assertEqual(
            (tg["media_s"], tg["format"], tg["format_guessed"], tg["post_format"]), (40.0, "vertical", True, "video")
        )
        self.assertIn("weekday", tg)
        self.assertEqual(records["download-16x9"]["media_s"], 300.0)

    def test_backfill_idempotent_and_ordered(self):
        created = self.make_project()
        first = backfill.run(self.dir)
        got = self.log.read()
        self.assertEqual(first["added"], len(got))
        kinds = [(e["event"], e.get("step")) for e in got]
        self.assertIn(("project.created", None), kinds)
        self.assertIn(("project.archived", None), kinds)
        self.assertEqual(kinds.count(("step.started", "text")), 2)
        self.assertIn(("step.failed", "text"), kinds)
        # voice в логе нет — берётся из состояния; publish ждёт — тоже
        self.assertIn(("step.finished", "voice"), kinds)
        self.assertIn(("step.waiting", "publish"), kinds)
        self.assertIn(("publication.sent", "publish"), kinds)
        self.assertIn(("publication.packaged", "publish"), kinds)
        self.assertEqual(sum(e["event"] == "stats.snapshot" for e in got), 2)
        self.assertTrue(all(e["source"] == "backfill" for e in got))
        # Время после полуночи — следующий день.
        failed = next(e for e in got if e["event"] == "step.failed")
        self.assertAlmostEqual(failed["ts"], created + 240, delta=1)

        again = backfill.run(self.dir)
        self.assertEqual((again["added"], len(self.log.read())), (0, len(got)))

    def test_backfill_skips_after_first_live_event(self):
        created = self.make_project()
        self.log.emit("step.started", ts=created + 30, project_id="aaaaaaaaaaaa", step="text")
        backfill.run(self.dir)
        got = [e for e in self.log.read() if e.get("source") == "backfill"]
        self.assertTrue(got)
        self.assertTrue(all(e["ts"] < created + 30 for e in got))


if __name__ == "__main__":
    unittest.main()
