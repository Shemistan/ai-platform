"""Наблюдаемость «Монтажа» (ITERATIVE_REVIEW_SPEC §14, PRD-028): события, дерево, ETA, ресурсы, API, SSE."""

import http.client
import tempfile
import time
import unittest
from pathlib import Path

from aiplatform.montage import progress, schemas
from aiplatform.montage.service import MontageModule
from tests.fixtures import FAST, NEEDS_FFMPEG
from tests.helpers import PlatformCase, scaled


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)

    def events(self, **kw) -> progress.Events:
        ev = progress.Events(self.folder, "p1", **kw)
        self.addCleanup(ev.close)
        return ev

    def test_append_cursor_filters_export_and_restart(self):
        ev = self.events()
        first = ev.append("run_queued", status="queued", message="старт", run_id="r1", phase_id="render")
        self.assertEqual(schemas.validate("run_event", first), [])
        self.assertEqual((first["seq"], first["event_id"], first["correlation_id"]), (1, "p1-1", "r1"))
        ev.append("stage_progress", status="running", run_id="r1", stage_id="segments",
                  progress={"completed_units": 3, "total_units": 10, "unit": "segments"})  # fmt: skip
        ev.append(
            "job_failed", status="failed", severity="error", run_id="r1", job_id="j1", message="упал", reason="код 1"
        )
        page = ev.read(cursor=0, limit=2)
        self.assertEqual(([e["seq"] for e in page["events"]], page["has_more"], page["next_cursor"]), ([1, 2], True, 2))
        page = ev.read(cursor=2)
        self.assertEqual([e["type"] for e in page["events"]], ["job_failed"])
        self.assertEqual(ev.read(filters={"severity": "error"})["events"][0]["seq"], 3)
        self.assertEqual(ev.read(filters={"q": "упал"})["events"][0]["seq"], 3)
        self.assertEqual(len(ev.read(filters={"stage_id": "segments"})["events"]), 1)
        self.assertEqual(ev.read(filters={"min_severity": "warning"})["events"][0]["type"], "job_failed")
        jsonl = "".join(ev.export("jsonl"))
        self.assertEqual(len(jsonl.strip().splitlines()), 3)
        csv_text = "".join(ev.export("csv", {"severity": "error"}))
        self.assertIn("seq,occurred_at", csv_text.splitlines()[0])
        self.assertEqual(len(csv_text.strip().splitlines()), 2)
        # Рестарт: seq продолжается, не начинается заново.
        again = self.events()
        self.assertEqual(again.last_seq, 3)
        self.assertEqual(again.append("log", message="после рестарта")["seq"], 4)
        self.assertTrue(ev.throttled("k"))
        self.assertFalse(ev.throttled("k"))

    def test_rotation_keeps_order_and_hundred_thousand_events_page_fast(self):
        ev = self.events(rotate_lines=60_000)
        started = time.monotonic()
        for n in range(100_000):
            ev.append("stage_progress", status="running", run_id="r1", stage_id="segments", message=f"кусок {n}")
        # Лимиты времени — по стенным часам, как poll(): под параллельным прогоном масштабируются.
        self.assertLess(time.monotonic() - started, scaled(60), "100 000 событий пишутся без заметных тормозов")
        self.assertTrue((self.folder / "events.jsonl.1").is_file(), "ротация по числу строк")
        self.assertEqual(ev.last_seq, 100_000)
        t0 = time.monotonic()
        page = ev.read(cursor=99_500, limit=200)
        self.assertEqual(page["events"][0]["seq"], 99_501)
        self.assertEqual(len(page["events"]), 200)
        self.assertTrue(page["has_more"])
        self.assertLess(time.monotonic() - t0, scaled(3), "страница по курсору читается быстро")
        # Порядок сквозной через ротацию; фильтр по тексту находит ранние события из хвоста.
        seqs = [e["seq"] for e in ev.iter_all()]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(seqs[0], 1, "хвост до ротации читается вместе с текущим файлом")
        self.assertEqual(ev.read(filters={"q": 'кусок 7"'}, limit=5)["events"][0]["seq"], 8)
        self.assertEqual(self.events(rotate_lines=60_000).last_seq, 100_000)


class TreeTest(unittest.TestCase):
    def events(self):
        base = {"project_id": "p1", "severity": "info", "metrics": {}, "artifact_ids": [], "reason": "", "attempt": 0,
                "progress": None, "correlation_id": "r1", "run_id": "r1", "phase_id": "render", "round_id": None,
                "agent_id": None, "job_id": None, "task_id": None}  # fmt: skip

        def ev(seq, t, type_, status, **kw):
            return {**base, "seq": seq, "event_id": f"p1-{seq}", "occurred_at": 1000 + t, "type": type_, "status": status,
                    "message": kw.pop("message", ""), **kw}  # fmt: skip

        return [
            ev(1, 0, "run_queued", "queued", stage_id=None, metrics={"stages": ["segments", "final"]}),
            ev(2, 1, "run_started", "running", stage_id=None),
            ev(3, 1, "stage_started", "running", stage_id="segments"),
            ev(
                4,
                2,
                "stage_progress",
                "running",
                stage_id="segments",
                progress={"completed_units": 2, "total_units": 4, "unit": "segments"},
            ),
            ev(5, 3, "job_started", "running", stage_id="segments", job_id="j1", agent_id="ffmpeg", attempt=1),
            ev(
                6,
                4,
                "job_finished",
                "completed",
                stage_id="segments",
                job_id="j1",
                agent_id="ffmpeg",
                attempt=1,
                metrics={"render_fps": 20},
            ),
            ev(
                7,
                5,
                "stage_finished",
                "completed",
                stage_id="segments",
                progress={"completed_units": 100, "total_units": 100, "unit": "percent"},
            ),
            ev(8, 5, "stage_started", "running", stage_id="scenes"),
            ev(9, 6, "scene_rendered", "completed", stage_id="scenes", task_id="scene_001"),
            ev(
                10,
                7,
                "scene_rendered",
                "failed",
                stage_id="scenes",
                task_id="scene_002",
                reason="рендер упал",
                severity="error",
                attempt=2,
            ),
        ]

    def test_tree_hierarchy_counts_and_statuses(self):
        project = {"id": "p1", "title": "Ролик", "status": "running", "run_id": "r1", "action": "render",
                   "stages": [{"id": "segments", "status": "done", "percent": 100, "weight": 25, "started_at": 1001, "finished_at": 1005},
                              {"id": "scenes", "status": "running", "percent": 50, "weight": 15, "started_at": 1005, "finished_at": None}]}  # fmt: skip
        root = progress.tree(project, self.events(), now=1010)
        self.assertEqual(
            (root["kind"], root["status"], root["percent"]), ("project", "running", None), "100 % только после проверки"
        )
        run = root["children"][0]
        self.assertEqual((run["kind"], run["status"], run["planned_stages"]), ("run", "running", ["segments", "final"]))
        phases = {p["title"]: p for p in run["children"]}
        self.assertEqual(set(phases), {"render", "motion"})
        segments = phases["render"]["children"][0]
        self.assertEqual(
            (segments["status"], segments["percent"], segments["units"]["unit"]), ("completed", 100.0, "segments")
        )
        job = segments["children"][0]
        self.assertEqual(
            (job["kind"], job["status"], job["metrics"]["render_fps"], job["attempt"]), ("job", "completed", 20, 1)
        )
        scenes = phases["motion"]["children"][0]
        self.assertEqual(scenes["percent"], 50, "идущая стадия берёт процент из project.stages")
        tasks = {t["title"]: t for t in scenes["children"]}
        self.assertEqual(tasks["scene_002"]["blocking_reason"], "рендер упал")
        self.assertEqual(tasks["scene_002"]["children"][0]["title"], "попытка 2")
        self.assertEqual(
            scenes["counts"], {"total": 2, "completed": 1, "running": 0, "failed": 1, "skipped": 0, "remaining": 1}
        )
        self.assertEqual(run["counts"]["total"], 2)
        self.assertEqual(root["last_updated"], 1007)
        self.assertEqual(segments["elapsed_s"], 4.0)
        done = progress.tree({**project, "status": "done", "stages": []}, self.events(), now=1010)
        self.assertEqual((done["status"], done["percent"]), ("completed", 100.0))

    def test_eta_uses_history_with_confidence_and_falls_back_to_weights(self):
        with tempfile.TemporaryDirectory() as tmp:
            stats = progress.Stats(Path(tmp) / "stats.json")
            stage_list = [{"id": "segments", "status": "running", "percent": 50, "weight": 25, "started_at": 100, "finished_at": None},
                          {"id": "final", "status": "pending", "percent": 0, "weight": 8, "started_at": None, "finished_at": None}]  # fmt: skip
            eta = progress.eta(stage_list, stats, now=110)
            self.assertEqual((eta["confidence"], eta["basis"]), ("low", "weights"))
            self.assertIsNotNone(eta["seconds"])
            for _ in range(12):
                stats.observe("final", 16.0, 8)
            self.assertEqual(stats.per_weight("final"), (2.0, 12))
            eta = progress.eta(stage_list, stats, now=110)
            self.assertEqual((eta["basis"], eta["confidence"]), ("history", "high"))
            self.assertEqual(eta["seconds"], 10 + 16, "текущая по скорости (10 с осталось) + история финала")
            self.assertLess(eta["low"], eta["seconds"])
            self.assertGreater(eta["high"], eta["seconds"])
            self.assertEqual(
                progress.eta(
                    [{"id": "x", "status": "done", "percent": 100, "weight": 1, "started_at": 1, "finished_at": 2}],
                    stats,
                )["basis"],
                "idle",
            )
            reloaded = progress.Stats(Path(tmp) / "stats.json")
            self.assertEqual(reloaded.per_weight("final")[1], 12)

    def test_resources_only_measurable(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = progress.resources(Path(tmp), queue_depth=2, gpu_waiting=1, gpu_holder="Монтаж",
                                     last_job={"job_id": "j", "metrics": {"render_fps": 18.5}}, retries=1)  # fmt: skip
        self.assertEqual(res["gpu"]["status"], "unavailable")
        self.assertIn("Монтаж", res["gpu"]["reason"])
        self.assertEqual(res["queue"], {"depth": 2, "gpu_waiting": 1})
        self.assertEqual(res["render"]["fps"], 18.5)
        self.assertIn("free_bytes", res["disk"])
        self.assertIn("load_1m", res["cpu"])
        self.assertEqual(progress.ffmpeg_metrics("fps= 24.5"), {"render_fps": 24.5})
        self.assertEqual(progress.ffmpeg_metrics("speed=1.5x"), {"speed": 1.5})
        self.assertIsNone(progress.ffmpeg_metrics("frame=12"))


def montage_only(ctx):
    return [MontageModule(ctx)]


@NEEDS_FFMPEG
class ProgressApiTest(PlatformCase):
    modules = staticmethod(montage_only)
    process_env = FAST

    @classmethod
    def setUpClass(cls):
        from tests.test_montage import make_video

        cls.media = tempfile.TemporaryDirectory()
        cls.head = Path(cls.media.name) / "head.mp4"
        make_video(cls.head, 8)

    @classmethod
    def tearDownClass(cls):
        cls.media.cleanup()

    def settle(self, pid, statuses, timeout=240):
        return self.poll(f"/api/montage/projects/{pid}", lambda p: p["status"] in statuses, timeout=timeout)

    def test_events_progress_sse_export_and_restart(self):
        base = "/api/montage/projects"
        pid = self.json("POST", base, {"title": "Прогресс", "options": {"check": False, "mode": "autonomous"}})[1]["id"]
        self.assertEqual(self.json("POST", f"{base}/{pid}/sources/head", self.head.read_bytes())[0], 201)
        _, empty = self.json("GET", f"{base}/{pid}/progress")
        self.assertEqual((empty["tree"]["kind"], empty["tree"]["children"], empty["status"]), ("project", [], "draft"))
        self.json("POST", f"{base}/{pid}/start")
        done = self.settle(pid, ("done", "failed"))
        self.assertEqual(done["status"], "done", done["error"])
        _, page = self.json("GET", f"{base}/{pid}/events?limit=1000")
        types = [e["type"] for e in page["events"]]
        self.assertEqual(types[0], "run_queued")
        self.assertIn("run_started", types)
        self.assertIn("stage_started", types)
        self.assertIn("job_started", types)
        self.assertIn("job_finished", types)
        self.assertIn("stage_finished", types)
        self.assertEqual(types[-1], "run_finished")
        self.assertEqual(page["events"][-1]["status"], "completed")
        seqs = [e["seq"] for e in page["events"]]
        self.assertEqual(seqs, list(range(1, len(seqs) + 1)), "seq сквозной и без дыр")
        self.assertTrue(all(schemas.validate("run_event", e) == [] for e in page["events"]))
        self.assertTrue(any(e["agent_id"] == "asr" for e in page["events"]))
        self.assertTrue(any(e["agent_id"] == "ffmpeg" and e["type"] == "job_progress" for e in page["events"]))
        # Фильтры и курсор.
        _, errors = self.json("GET", f"{base}/{pid}/events?severity=error")
        self.assertEqual(errors["events"], [])
        _, tail = self.json("GET", f"{base}/{pid}/events?cursor={seqs[-3]}")
        self.assertEqual([e["seq"] for e in tail["events"]], seqs[-2:])
        # Дерево: 100 % только у завершённого; стадии с подпроцессами.
        _, prog = self.json("GET", f"{base}/{pid}/progress")
        tree = prog["tree"]
        self.assertEqual((tree["status"], tree["percent"]), ("completed", 100.0))
        run = tree["children"][0]
        self.assertEqual(run["status"], "completed")
        stages_ = [n for p in run["children"] for n in p["children"] if n["kind"] == "stage"]
        self.assertTrue(stages_ and all(s["status"] in ("completed", "skipped") for s in stages_), stages_)
        self.assertTrue(any(n["kind"] == "job" for s in stages_ for n in s["children"]))
        # Агенты, история запусков, ресурсы.
        _, agents = self.json("GET", f"{base}/{pid}/agents")
        ids = {a["id"] for a in agents["agents"]}
        self.assertTrue({"asr", "ffmpeg", "rough_cut"} <= ids, ids)
        self.assertTrue(all(a["done"] >= 1 for a in agents["agents"] if a["id"] in ("asr", "ffmpeg")))
        _, runs = self.json("GET", f"{base}/{pid}/runs")
        self.assertEqual((runs["runs"][0]["status"], runs["runs"][0]["action"]), ("completed", "ingest"))
        self.assertIsNotNone(runs["runs"][0]["duration_s"])
        _, res = self.json("GET", f"{base}/{pid}/resources")
        self.assertEqual(res["gpu"]["status"], "unavailable")
        # Экспорт JSONL и CSV.
        status, headers, body = self.call("GET", f"{base}/{pid}/logs/export")
        self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "application/x-ndjson"))
        self.assertEqual(len(body.decode().strip().splitlines()), len(seqs))
        status, headers, csv_body = self.call("GET", f"{base}/{pid}/logs/export?format=csv&severity=info")
        self.assertEqual(status, 200)
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertTrue(csv_body.startswith(b"seq,occurred_at"))
        # SSE: снапшот, backfill после Last-Event-ID без дублей, heartbeat не ломает поток.
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        conn.request("GET", f"{base}/{pid}/events/stream", headers={"Last-Event-ID": str(seqs[-4])})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/event-stream; charset=utf-8")
        got = b""
        while b"id: %d\n" % seqs[-1] not in got:
            chunk = resp.readline()
            if not chunk:
                break
            got += chunk
        conn.close()
        text = got.decode()
        self.assertIn("event: snapshot", text)
        ids_seen = [int(x) for x in __import__("re").findall(r"^id: (\d+)$", text, flags=__import__("re").M)]
        self.assertEqual(ids_seen[0], seqs[-4], "снапшот помечен курсором клиента")
        self.assertEqual(ids_seen[1:], seqs[-3:], "backfill — только после Last-Event-ID, без дублей")
        # Рестарт: события и дерево читаются с диска новым экземпляром.
        from aiplatform.montage.projects import MontageProjects

        module = self.app.modules["montage"]
        again = MontageProjects(module.config, module.ctx.gate)
        try:
            self.assertEqual(again.events_of(pid).last_seq, seqs[-1])
            self.assertEqual(again.observe.tree(pid)["tree"]["status"], "completed")
        finally:
            again.close()

    def test_failed_run_is_visible_with_reason(self):
        base = "/api/montage/projects"
        pid = self.json("POST", base, {"title": "Сбой", "options": {"check": False}})[1]["id"]
        self.assertEqual(self.json("POST", f"{base}/{pid}/sources/head", self.head.read_bytes())[0], 201)
        import os

        os.environ["FAKE_ASR_FAIL"] = "1"
        self.addCleanup(os.environ.pop, "FAKE_ASR_FAIL", None)
        self.json("POST", f"{base}/{pid}/start")
        failed = self.settle(pid, ("done", "failed"))
        self.assertEqual(failed["status"], "failed")
        _, page = self.json("GET", f"{base}/{pid}/events?severity=error")
        types = [e["type"] for e in page["events"]]
        self.assertIn("job_failed", types)
        self.assertIn("run_failed", types)
        self.assertTrue(page["events"][-1]["reason"])
        _, prog = self.json("GET", f"{base}/{pid}/progress")
        self.assertEqual(prog["tree"]["status"], "failed")
        run = prog["tree"]["children"][0]
        self.assertEqual(run["status"], "failed")
        self.assertTrue(run["blocking_reason"])
        _, res = self.json("GET", f"{base}/{pid}/resources")
        self.assertGreaterEqual(res["retries"], 1)


if __name__ == "__main__":
    unittest.main()
