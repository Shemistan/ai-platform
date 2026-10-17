"""Вкладка «Каналы»: каналы публикации, бот, подписчики и приём подписок — на фейковом движке."""

import time
import unittest

from aiplatform.channels.service import ChannelsModule
from tests.helpers import PlatformCase


def channels_only(ctx):
    return [ChannelsModule(ctx)]


class ChannelsTest(PlatformCase):
    modules = staticmethod(channels_only)
    process_env = {"FAKE_BOT_POLL": "0.1"}

    def test_state_without_own_list(self):
        status, body = self.json("GET", "/api/channels/state")
        self.assertEqual(status, 200, body)
        self.assertFalse(body["custom"])
        self.assertEqual([c["type"] for c in body["channels"]], ["manual", "manual"])
        self.assertTrue(body["bot"]["token"])
        self.assertEqual({c["label"] for c in body["importable"]}, {"Telegram: HISTORY_CHAN", "Telegram: VOICE_NEWS"})
        self.assertEqual(body["unsent"], 1)
        self.assertFalse(body["serve"]["running"])

    def test_add_edit_import_delete(self):
        status, body = self.json(
            "POST", "/api/channels/channels", {"label": "Мой", "chat_id": "@my_chan", "format": "vertical"}
        )
        self.assertEqual(status, 201, body)
        added = body["added"]
        self.assertTrue(body["custom"])
        self.assertEqual(self.json("POST", "/api/channels/channels", {"label": "Мой", "chat_id": "плохой"})[0], 400)

        _, body = self.json("POST", f"/api/channels/channels/{added}", {"label": "Шортсы", "token_env": "AIP_TG_SH"})
        mine = next(c for c in body["channels"] if c["id"] == added)
        self.assertEqual((mine["label"], mine["format"], mine["token_env"]), ("Шортсы", "vertical", "AIP_TG_SH"))
        self.assertIn("AIP_TG_SH", mine["problem"])
        self.assertEqual(self.json("POST", f"/api/channels/channels/{added}", {"token_env": "плохо"})[0], 400)
        # Чужие секреты окружения в токен канала не берём (SEC-006).
        for name in ("HF_TOKEN", "ANTHROPIC_API_KEY", "NARRATOR_TELEGRAM_TOKEN_FILE", "AIP_TG_"):
            status, body = self.json("POST", f"/api/channels/channels/{added}", {"token_env": name})
            self.assertEqual(status, 400, name)
            self.assertIn("AIP_TG_", body["error"])
        status, _ = self.json(
            "POST", "/api/channels/channels", {"label": "Чужой", "chat_id": "@other", "token_env": "HF_TOKEN"}
        )
        self.assertEqual(status, 400)
        self.assertEqual(self.json("POST", "/api/channels/channels/nope", {"label": "x"})[0], 404)

        _, body = self.json("POST", "/api/channels/channels/import")
        self.assertEqual(len(body["imported"]), 2)
        _, body = self.json("POST", "/api/channels/channels/import")
        self.assertEqual(body["imported"], [])
        _, state = self.json("GET", "/api/channels/state")
        self.assertEqual(state["importable"], [])

        _, body = self.json("POST", f"/api/channels/channels/{added}/delete")
        self.assertNotIn(added, [c["id"] for c in body["channels"]])
        self.assertEqual(self.json("POST", f"/api/channels/channels/{added}/delete")[0], 404)

    def test_bot_check_and_subscribers(self):
        _, bot = self.json("POST", "/api/channels/bot/check")
        self.assertEqual(bot["username"], "demo_narrator_bot")
        _, subs = self.json("GET", "/api/channels/subscribers?sync=1")
        self.assertEqual((subs["active"], len(subs["subscribers"])), (2, 3))

    def test_serve_accepts_subscriptions(self):
        _, serve = self.json("POST", "/api/channels/serve/start")
        self.assertTrue(serve["running"])
        deadline = time.time() + 10
        while time.time() < deadline:
            _, subs = self.json("GET", "/api/channels/subscribers")
            if subs["active"] == 3:
                break
            time.sleep(0.1)
        self.assertEqual(subs["active"], 3, self.json("GET", "/api/channels/serve")[1])
        _, serve = self.json("GET", "/api/channels/serve")
        self.assertTrue(any("слушает" in line for line in serve["log"]), serve)
        _, serve = self.json("POST", "/api/channels/serve/stop")
        self.assertFalse(serve["running"])
        self.assertIsNone(serve["error"])


if __name__ == "__main__":
    unittest.main()
