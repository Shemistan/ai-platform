"""Очереди задач вкладок «Фото», «Видео», «Аудио» (BE-007): воркер не падает, список ограничен."""

import time
import unittest

from aiplatform.core import jobs as core_jobs
from tests.helpers import PlatformCase

QUEUES = ("photo", "video", "audio")


class SelectTest(unittest.TestCase):
    def test_limit_since_and_active(self):
        done = [{"id": str(i), "status": "done", "created_at": i, "finished_at": i + 0.5} for i in range(10)]
        running = {"id": "r", "status": "running", "created_at": 0.1, "started_at": 0.2, "finished_at": None}
        got = core_jobs.select([*done, running], limit=3)
        self.assertEqual([j["id"] for j in got], ["9", "8", "7"])
        got = core_jobs.select([*done, running], since=7.9)
        self.assertEqual([j["id"] for j in got], ["9", "8", "r"], "идущая — всегда")
        self.assertEqual(len(core_jobs.select(done)), 10, "без limit — все (уборка, сценарии)")


class ForgottenJobTest(PlatformCase):
    def test_worker_survives_job_forgotten_before_start(self):
        # «Отменили в очереди, уборка забыла задачу до воркера»: id в очереди есть, задачи нет.
        for prefix in QUEUES:
            with self.subTest(prefix=prefix):
                jobs = self.app.modules[prefix].jobs
                jobs._queue.put("0123456789ab")
                deadline = time.time() + 5
                while not jobs._queue.empty() and time.time() < deadline:
                    time.sleep(0.02)
                time.sleep(0.1)
                self.assertTrue(jobs._worker.is_alive(), "воркер жив после пропавшей задачи")

    def test_cancel_and_forget_queued_then_next_job_runs(self):
        jobs = self.app.modules["photo"].jobs
        body = {"prompt": "кот", "width": 512, "height": 512}
        first = jobs.create(body)
        jobs.cancel(first["id"])
        jobs.forget(first["id"])
        second = jobs.create(body)
        final = jobs.wait(second["id"])
        self.assertIn(final["status"], ("done", "failed"))
        self.assertTrue(jobs._worker.is_alive())


class JobListLimitTest(PlatformCase):
    def test_limit_and_bad_args(self):
        for _ in range(3):
            status, _ = self.json("POST", "/api/photo/jobs", {"prompt": "кот", "width": 512, "height": 512})
            self.assertEqual(status, 201)
        status, body = self.json("GET", "/api/photo/jobs?limit=2")
        self.assertEqual((status, len(body["jobs"])), (200, 2))
        self.assertEqual(len(self.json("GET", "/api/photo/jobs")[1]["jobs"]), 3)
        for query in ("limit=abc", "limit=0", "since=yesterday"):
            status, body = self.json("GET", f"/api/photo/jobs?{query}")
            self.assertEqual((status, body["code"]), (400, "bad_request"), query)
        for prefix in ("video", "audio"):
            self.assertEqual(self.json("GET", f"/api/{prefix}/jobs?limit=1")[0], 200)
