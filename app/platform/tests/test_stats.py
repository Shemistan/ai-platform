"""Отклик каналов: подписчики, просмотры, ручные просмотры и сводка «что работает»."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.studio import publish, stats
from tests.test_studio_channels import TOKEN
from tests.test_studio_flow import ARTICLE, StudioCase

EMBED = '<div class="tgme_widget_message_info"><span class="tgme_widget_message_views">1.2K</span></div>'


def fake_post(url, body, ctype):
    if url.endswith("/getChat"):
        return {"ok": True, "result": {"id": -100123, "username": "mychan"}}
    if url.endswith("/getChatMemberCount"):
        return {"ok": True, "result": 150}
    return {"ok": False, "description": "unexpected"}


class StatsUnitTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(
            [stats.parse_count(x) for x in ("15", "1.2K", "2M", "3,5K", "много")], [15, 1200, 2000000, 3500, None]
        )
        self.assertEqual(stats.views_from_embed(EMBED), 1200)
        self.assertIsNone(stats.views_from_embed("<html>нет</html>"))

    def test_refresher_and_growth(self):
        channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="-100123", token_env="AIP_TG_TEST")
        pages = []

        def get(url):
            pages.append(url)
            return EMBED

        record = {"channel": "tg", "ok": True, "message_id": 42, "members_at_publish": 120}
        with mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}):
            fresh = stats.Refresher({"tg": channel}, http_post=fake_post, get=get).record(record)
        self.assertEqual(pages, ["https://t.me/mychan/42?embed=1"])
        self.assertTrue(fresh["public"])
        self.assertEqual(stats.latest(fresh) | {"at": None}, {"views": 1200, "members": 150, "growth": 30, "at": None})
        # Без токена или id сообщения — ничего не трогаем.
        self.assertEqual(stats.Refresher({"tg": channel}).record(record), record)

    def test_summary_groups(self):
        def post(views, growth=None, fmt="landscape"):
            return {"views": views, "growth": growth, "format": fmt}

        items = [
            {"scenario_label": "Дайджест", "video_s": 120, "published": [post(100, 2)]},
            {"scenario_label": "Дайджест", "video_s": 600, "published": [post(300)]},
            {"scenario_label": "Статья", "video_s": 2000, "published": [post(50, 0, "vertical"), post(None)]},
        ]
        found = {d["dimension"]: d["rows"] for d in stats.summary(items)}
        self.assertEqual(found["Сценарий"][0], {"label": "Дайджест", "posts": 2, "avg_views": 200, "avg_growth": 2.0})
        self.assertEqual([r["label"] for r in found["Длина ролика"]], ["8–15 мин", "до 3 мин", "длиннее 15 мин"])
        self.assertEqual(found["Формат"][1]["label"], "вертикальный 9:16")


class StatsHttpTest(StudioCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        channels = [
            {"id": "tg", "label": "TG", "type": "telegram", "chat_id": "-100123", "token_env": "AIP_TG_TEST"},
            {"id": "yt", "label": "YouTube", "type": "manual"},
        ]
        (folder / "channels.json").write_text(json.dumps(channels), encoding="utf-8")
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(folder / "channels.json")}
        super().setUp()

    def test_refresh_manual_views_and_summary(self):
        project = self.create(scenario="article", text=ARTICLE)
        pid = project["id"]
        published = [
            {"channel": "tg", "ok": True, "message_id": 7, "members_at_publish": 100, "sent": 1.0},
            {"channel": "yt", "ok": True, "files": []},
        ]
        (self.folder(pid) / "published.json").write_text(json.dumps(published), encoding="utf-8")

        with (
            mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}),
            mock.patch.object(publish, "_http_post", fake_post),
            mock.patch.object(stats, "http_get", lambda url: EMBED),
        ):
            status, _ = self.json("POST", "/api/studio/stats/refresh")
            self.assertEqual(status, 202)
            board = self.poll("/api/studio/board", lambda b: not b["stats"]["running"] and b["stats"]["at"])
        tg = next(r for r in board["projects"][0]["published"] if r["channel"] == "tg")
        self.assertEqual((tg["views"], tg["growth"], tg["public"]), (1200, 50, True))

        status, view = self.json("POST", f"/api/studio/projects/{pid}/stats", {"channel": "yt", "views": 300})
        self.assertEqual(status, 200, view)
        for bad in ({"channel": "yt", "views": -1}, {"channel": "nope", "views": 1}, {"channel": "yt", "views": "10"}):
            self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/stats", bad)[0], 400, bad)
        board = self.json("GET", "/api/studio/board")[1]
        rows = {d["dimension"]: d["rows"] for d in board["response"]}
        self.assertEqual(
            rows["Сценарий"], [{"label": "Видео по статье", "posts": 2, "avg_views": 750, "avg_growth": 50.0}]
        )
        saved = json.loads((self.folder(pid) / "published.json").read_text(encoding="utf-8"))
        self.assertTrue(saved[1]["stats"][-1]["manual"])


if __name__ == "__main__":
    unittest.main()
