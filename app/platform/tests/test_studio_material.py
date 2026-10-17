"""Ключ материала, предупреждение о похожем выпуске и автоархив брошенных (PRD-004)."""

import time
import unittest

from aiplatform.core import cleanup
from aiplatform.studio.service import material_keys, norm_title, norm_url
from tests.fixtures import ARTICLE
from tests.helpers import StudioCase

DAY = 86400


class MaterialKeyTest(unittest.TestCase):
    def test_discord_and_cyrillic_spelling_give_one_key(self):
        self.assertEqual(
            material_keys("article", title="Discord запускает видеозвонки"),
            material_keys("article", title="Дискорд запускает видеозвонки!"),
        )

    def test_case_yo_punctuation_and_copy_suffix(self):
        self.assertEqual(norm_title("Ёлка: ИТОГИ года (копия)"), norm_title("елка — итоги года"))

    def test_generic_titles_give_no_key(self):
        self.assertEqual(material_keys("post", title="Пост в канал"), [])

    def test_url_first_then_title(self):
        keys = material_keys("article", title="GC в Go", url="https://www.Example.dev/gc/?utm_source=tg#top")
        self.assertEqual(keys[0], "url:example.dev/gc")
        self.assertTrue(keys[1].startswith("title:"))
        self.assertEqual(norm_url("http://example.dev/gc"), "example.dev/gc")

    def test_digest_by_day(self):
        self.assertEqual(material_keys("digest", day="2026-10-15", title="что угодно"), ["digest:2026-10-15"])


class SimilarCreateTest(StudioCase):
    def test_similar_title_warns_with_link(self):
        first = self.create(scenario="article", title="Discord запускает видеозвонки", text=ARTICLE)
        status, body = self.json(
            "POST",
            "/api/studio/projects",
            {"scenario": "article", "title": "Дискорд запускает видеозвонки", "text": ARTICLE + "\nПравка.\n"},
        )
        self.assertEqual(status, 409, body)
        self.assertIn("Похожий выпуск уже есть", body["error"])
        existing = body["existing"]
        self.assertEqual(existing["id"], first["id"])
        self.assertTrue(existing["similar"])
        self.assertIn(first["id"], existing["url"])

        body = {"scenario": "article", "title": "Дискорд запускает видеозвонки", "text": "Другой текст."}
        status, again = self.json("POST", "/api/studio/projects", body | {"force_new": True})
        self.assertEqual(status, 201, again)  # «Создать всё равно»

    def test_material_in_board_and_copy_shares_it(self):
        first = self.create(scenario="article", title="Discord", text=ARTICLE)
        status, copy = self.json("POST", f"/api/studio/projects/{first['id']}/clone", {})
        self.assertIn(status, (200, 201), copy)
        board = self.json("GET", "/api/studio/board")[1]["projects"]
        keys = {p["id"]: p["material"] for p in board}
        self.assertEqual(keys[first["id"]], keys[copy["id"]])
        self.assertTrue(keys[first["id"]].startswith(("url:", "title:")))


class StaleArchiveTest(StudioCase):
    @property
    def cleaner(self):
        return self.app.modules["settings"].cleaner

    def test_draft_idle_8_days_archived_6_days_not(self):
        pid = self.create(scenario="article", title="Брошенный", text=ARTICLE)["id"]
        self.assertEqual(self.cleaner.archive_stale(7, now=time.time() + 6 * DAY), [])
        self.assertFalse(self.app.modules["studio"].studio.get(pid).get("archived"))
        done = self.cleaner.archive_stale(7, now=time.time() + 8 * DAY)
        self.assertEqual([p["id"] for p in done], [pid])
        project = self.app.modules["studio"].studio.get(pid)
        self.assertTrue(project["archived"])
        self.assertEqual(project["archived_by"], cleanup.STALE_ARCHIVED)
        titles = [n["title"] for n in self.app.ctx.notifier.recent(10)]
        self.assertTrue(any("В архив без движения" in t for t in titles), titles)

    def test_zero_threshold_disables(self):
        pid = self.create(scenario="article", title="Брошенный", text=ARTICLE)["id"]
        self.assertEqual(self.cleaner.archive_stale(0, now=time.time() + 100 * DAY), [])
        self.assertFalse(self.app.modules["studio"].studio.get(pid).get("archived"))

    def test_daily_job_uses_setting(self):
        status, body = self.json("POST", "/api/settings/section/cleanup", {"draft_days": 0, "auto": False})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["cleanup"]["draft_days"], 0)
        self.create(scenario="article", title="Брошенный", text=ARTICLE)
        self.assertIsNone(self.app.modules["settings"]._daily_cleanup())
