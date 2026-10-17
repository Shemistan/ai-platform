"""SLI «Здоровья» по журналу событий (REL-010): синтетический журнал и выпуск на фейках."""

import unittest
from datetime import datetime, timedelta

from aiplatform.studio import metrics
from tests.fixtures import ARTICLE, NEEDS_FFMPEG
from tests.helpers import StudioCase

NOW = datetime(2026, 10, 17, 18, 0).timestamp()


def at(days_ago: int, hour: int, minute: int = 0) -> float:
    day = datetime(2026, 10, 17) - timedelta(days=days_ago)
    return day.replace(hour=hour, minute=minute).timestamp()


def step(ts, name, ok=True, duration=None):
    return {"ts": ts, "event": "step.finished" if ok else "step.failed", "step": name, "duration_s": duration}


def digest(pid, created, sent=None, actor="scheduler"):
    out = [{"ts": created, "event": "project.created", "scenario": "digest", "project_id": pid, "actor": actor}]
    if sent is not None:
        out.append({"ts": sent, "event": "publication.sent", "scenario": "digest", "project_id": pid})
    return out


class SliTest(unittest.TestCase):
    def events(self):
        out = [
            *digest("a", at(0, 7), at(0, 8)),  # сегодня — вовремя
            *digest("b", at(1, 7), at(1, 13)),  # вчера — после 12:00
            *digest("c", at(2, 7)),  # позавчера — не опубликован
            *digest("old", at(20, 7), at(20, 9)),  # только в 28-дневном окне
            *digest("ancient", at(40, 7), at(40, 9)),  # вне окон
        ]
        for n, duration in enumerate([60, 70, 80, 90, 100, 110, 120, 130, 140, 600]):
            out.append(step(at(n % 5, 7, 30), "render", duration=duration))
        out += [
            step(at(0, 7, 40), "publish"),
            step(at(1, 7, 40), "publish", ok=False),
            step(at(2, 7, 40), "images", ok=False),
        ]
        out.append({"ts": at(0, 7), "event": "step.waiting", "step": "publish"})  # не итог — не считается
        return out

    def test_windows(self):
        week = metrics.sli(self.events(), 7, now=NOW)
        self.assertEqual((week["steps"], week["failed_steps"]), (13, 2))
        self.assertEqual(week["failed_share"], round(2 / 13, 3))
        self.assertEqual(week["publish_failed_share"], 0.5)
        self.assertEqual(week["render_builds"], 10)
        self.assertEqual((week["render_p50_s"], week["render_p90_s"]), (100, 140))
        self.assertEqual(week["failed_by_step"], {"publish": 1, "images": 1})
        # Окно — с первого события в нём (позавчера… 4 дня назад у сборок): 5 дней, вовремя — один.
        self.assertEqual((week["digest_on_time_days"], week["digest_days"]), (1, 5))
        self.assertEqual(week["scheduled_digests"], 3)
        month = metrics.sli(self.events(), 28, now=NOW)
        self.assertEqual((month["digest_on_time_days"], month["digest_days"]), (2, 21))
        later = metrics.sli(self.events(), 7, now=NOW, deadline="14:00")
        self.assertEqual(later["digest_on_time_days"], 2)

    def test_empty(self):
        empty = metrics.sli([], 7, now=NOW)
        self.assertIsNone(empty["failed_share"])
        self.assertIsNone(empty["render_p90_s"])
        self.assertEqual(empty["digest_days"], 1)
        self.assertEqual(metrics.percentile([5], 0.9), 5)


@NEEDS_FFMPEG
class HealthStatsTest(StudioCase):
    def test_run_shows_in_health(self):
        project = self.create(scenario="article", text=ARTICLE, run=True)
        self.settle(project["id"])
        events = self.app.modules["studio"].events.read()
        render = [e for e in events if e.get("event") == "step.finished" and e.get("step") == "render"]
        self.assertEqual(len(render), 1)
        self.assertGreater(render[0]["duration_s"], 0)
        status, body = self.json("GET", "/api/studio/health/stats")
        self.assertEqual(status, 200, body)
        week = body["windows"]["7"]
        self.assertEqual(week["failed_steps"], 0)
        self.assertGreater(week["steps"], 5)
        self.assertGreater(week["render_p90_s"], 0)
        self.assertEqual(body["deadline"], "12:00")


if __name__ == "__main__":
    unittest.main()
