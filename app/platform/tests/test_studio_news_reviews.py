"""Новости (PRD-033): обзоры недели и месяца — сценарии студии; ориентир длины видео target_minutes (не потолок)."""

from datetime import date

from aiplatform.studio import prompts
from aiplatform.studio import service as service_module
from tests.fixtures import NEEDS_FFMPEG
from tests.helpers import StudioCase


class ReviewPeriodTest(StudioCase):
    def test_periods_and_limits(self):
        day = date(2026, 10, 31)
        self.assertEqual(service_module.review_period("weekly", None, day), "2026-W44")
        self.assertEqual(service_module.review_period("monthly", "", day), "2026-10")
        self.assertEqual(service_module.review_period("monthly", "2026-09", day), "2026-09")
        _, cfg = self.json("GET", "/api/studio/config")
        minutes = {k: cfg["scenario_defaults"][k].get("target_minutes") for k in ("digest", "weekly", "monthly")}
        self.assertEqual(minutes, {"digest": 10, "weekly": 20, "monthly": 40})
        self.assertEqual(cfg["defaults"]["target_minutes"], 0)  # статья и пост — без ориентира

    def test_target_is_a_hint_not_a_cap(self):
        prompt = prompts.narration("Текст.", "Обзор", 20)
        self.assertIn("около 20 минут", prompt)
        self.assertIn("не жёсткий предел", prompt)
        self.assertNotIn("Ориентир", prompts.narration("Текст.", "Статья"))

    def test_bad_month_and_target(self):
        status, body = self.json("POST", "/api/studio/projects", {"scenario": "monthly", "month": "2026-13"})
        self.assertEqual(status, 400, body)
        status, body = self.json("POST", "/api/studio/projects", {"scenario": "monthly", "month": "2999-01"})
        self.assertEqual(status, 400, body)
        bad = {"scenario": "weekly", "settings": {"target_minutes": -1}}
        self.assertEqual(self.json("POST", "/api/studio/projects", bad)[0], 400)

    def test_same_week_is_a_duplicate(self):
        first = self.create(scenario="weekly")
        self.assertTrue(first["title"].startswith("Обзор недели "))
        status, body = self.json("POST", "/api/studio/projects", {"scenario": "weekly"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["existing"]["id"], first["id"])


@NEEDS_FFMPEG
class ReviewFlowTest(StudioCase):
    def test_monthly_text_from_teleprompter(self):
        project = self.create(scenario="monthly", month="2026-09", run=True)
        pid = project["id"]
        self.assertEqual(project["title"], "Обзор месяца 2026-09")
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["text"], "done", done["log"])
        folder = self.folder(pid)
        self.assertIn("обзор", (folder / "source.md").read_text(encoding="utf-8").lower())
        self.assertTrue((folder / "text.md").read_text(encoding="utf-8").strip())
        self.assertIn("Обзор", (folder / "post.md").read_text(encoding="utf-8"))
        log = "\n".join(done["log"])
        self.assertIn("обзор:", log)
        self.assertIn("при ориентире 40 мин", log)  # ориентир виден, текст не ужимается
