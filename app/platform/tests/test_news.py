"""Вкладка «Новости»: источники, главное за день, добор и обзоры — на фейковом движке."""

import unittest

from aiplatform.news.service import NewsModule, progress
from tests.helpers import PlatformCase


def news_only(ctx):
    return [NewsModule(ctx)]


class NewsUnitTest(unittest.TestCase):
    def test_progress(self):
        self.assertEqual(progress("  ок    go_blog                    материалов   5"), "ок: go_blog")
        self.assertEqual(progress("  сбой  uber                       HTTP 404"), "сбой: uber")
        self.assertEqual(progress("агент пишет суфлёр"), "Агент работает")
        self.assertIsNone(progress("что-то"))


class NewsTest(PlatformCase):
    modules = staticmethod(news_only)
    process_env = {"FAKE_NARRATOR_DELAY": "0.01"}

    def task(self, task):
        return self.poll(f"/api/news/tasks/{task['id']}", lambda t: t["status"] not in ("queued", "running"))

    def test_sources_and_collect(self):
        status, data = self.json("GET", "/api/news/sources")
        self.assertEqual(status, 200, data)
        by_id = {s["id"]: s for s in data["sources"]}
        self.assertTrue(by_id["postgres_news"]["failed"])
        self.assertTrue(by_id["random_newsletter"]["useless"])
        self.assertFalse(by_id["old_blog"]["enabled"])

        _, task = self.json("POST", "/api/news/sources/collect", {"only": "go_blog"})
        done = self.task(task)
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(done["title"], "Сбор: go_blog")
        self.assertTrue(any("go_blog" in line for line in done["log"]))
        self.assertEqual(self.json("POST", "/api/news/sources/collect", {"only": "Bad Id"})[0], 400)
        _, task = self.json("POST", "/api/news/sources/collect", {})
        self.assertEqual(self.task(task)["status"], "done")

    def test_top_with_issue(self):
        _, data = self.json("GET", "/api/news/top?day=2026-10-01")
        self.assertEqual(data["events"][0]["importance"], 9)
        self.assertIn("exists", data["issue"])
        self.assertEqual(self.json("GET", "/api/news/top?day=2999-01-01")[0], 400)
        self.assertEqual(self.json("GET", "/api/news/top?day=yesterday")[0], 400)

    def test_rescue(self):
        _, data = self.json("GET", "/api/news/failed")
        self.assertEqual({f["source"] for f in data["failed"]}, {"postgres_news", "uber_engineering"})
        _, task = self.json("POST", "/api/news/failed/rescue", {"day": "2026-10-01"})
        self.assertIn("за 2026-10-01", task["title"])
        self.assertEqual(self.task(task)["status"], "done")
        _, data = self.json("GET", "/api/news/failed")
        self.assertEqual(data["failed"], [])

    def test_reviews(self):
        _, data = self.json("GET", "/api/news/reviews")
        self.assertEqual(len(data["reviews"]), 1)
        _, task = self.json("POST", "/api/news/reviews/monthly", {"month": "2026-09"})
        self.assertEqual(self.task(task)["status"], "done")
        _, data = self.json("GET", "/api/news/reviews")
        monthly = next(r for r in data["reviews"] if r["kind"] == "monthly")
        _, review = self.json("GET", f"/api/news/reviews/{monthly['id']}")
        self.assertIn("teleprompter.md", review["files"])
        self.assertEqual(self.json("POST", "/api/news/reviews/monthly", {"month": "сентябрь"})[0], 400)
        self.assertEqual(self.json("GET", "/api/news/reviews/nope")[0], 404)
        _, task = self.json("POST", "/api/news/reviews/weekly", {})
        self.assertEqual(self.task(task)["status"], "done")


if __name__ == "__main__":
    unittest.main()
