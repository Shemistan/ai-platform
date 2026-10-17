"""Обложка дайджеста: ждать свою до срока, потом рисовать локально (PRD-002)."""

from datetime import datetime, timedelta
from unittest import mock

from tests.fixtures import NEEDS_FFMPEG, png_bytes
from tests.helpers import StudioCase


def tomorrow(hhmm: str) -> datetime:
    hour, minute = map(int, hhmm.split(":"))
    return (datetime.now() + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)


@NEEDS_FFMPEG
class PosterDeadlineTest(StudioCase):
    @property
    def module(self):
        return self.app.modules["studio"]

    def photo_jobs(self) -> int:
        return len(self.json("GET", "/api/photo/jobs")[1]["jobs"])

    def waiting_digest(self, day: str) -> str:
        project = self.create(scenario="digest", day=day, settings={"poster_deadline": "09:00"}, run=True)
        self.assertEqual(project["settings"]["poster_mode"], "upload")
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["poster"], "waiting", done["log"])
        return project["id"]

    def test_after_deadline_poster_goes_local_and_pipeline_continues(self):
        pid = self.waiting_digest("2026-10-05")
        self.assertEqual(self.module.poster_deadlines(tomorrow("08:59")), [], "до срока — ждём")
        before = self.photo_jobs()
        done = self.module.poster_deadlines(tomorrow("09:01"))
        self.assertEqual(len(done), 1, done)
        project = self.settle(pid)
        self.assertEqual(self.statuses(project)["poster"], "done", project["log"])
        self.assertEqual(project["settings"]["poster_mode"], "local")
        self.assertGreater(self.photo_jobs(), before, "постер нарисовала локальная модель")
        self.assertTrue(any("не дождались" in line for line in project["log"]), project["log"])
        notes = [n["title"] for n in self.app.ctx.notifier.recent(10)]
        self.assertTrue(any("не дождались" in t for t in notes), notes)
        self.assertEqual(self.module.poster_deadlines(tomorrow("09:02")), [], "второй раз не переключает")

    def test_upload_before_deadline_keeps_local_model_idle(self):
        pid = self.waiting_digest("2026-10-06")
        status, _, _ = self.call("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())
        self.assertEqual(status, 200)
        project = self.settle(pid)
        self.assertEqual(self.statuses(project)["poster"], "done", project["log"])
        before = self.photo_jobs()
        self.assertEqual(self.module.poster_deadlines(tomorrow("10:00")), [])
        self.assertEqual(self.photo_jobs(), before)
        self.assertEqual(self.settle(pid)["settings"]["poster_mode"], "upload")

    def test_scheduler_tick_checks_deadlines(self):
        now = tomorrow("09:01")
        with mock.patch.object(self.module, "poster_deadlines", return_value=["x"]) as check:
            self.assertIn("x", self.module.scheduler.tick(now))
        check.assert_called_once_with(now)

    def test_setting_in_form_defaults_and_validation(self):
        status, body = self.json("POST", "/api/settings/section/digest_defaults", {"poster_deadline": "08:30"})
        self.assertEqual(status, 200, body)
        project = self.create(scenario="digest", day="2026-10-07")
        self.assertEqual(project["settings"]["poster_deadline"], "08:30")
        status, _ = self.json("POST", "/api/settings/section/digest_defaults", {"poster_deadline": "25:00"})
        self.assertEqual(status, 400)
        status, _ = self.json(
            "POST",
            "/api/studio/projects",
            {"scenario": "digest", "day": "2026-10-08", "settings": {"poster_deadline": "x"}},
        )
        self.assertEqual(status, 400)
