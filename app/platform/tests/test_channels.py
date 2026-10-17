"""Вкладка «Каналы»: каналы публикации, бот, подписчики и приём подписок — на фейковом движке."""

import contextlib
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.channels.service import ChannelsModule
from aiplatform.channels.store import ChannelsBroken, ChannelStore
from aiplatform.studio import publish
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

    def test_add_keeps_token_env(self):
        status, body = self.json(
            "POST", "/api/channels/channels", {"label": "Свой бот", "chat_id": "@own_chan", "token_env": "AIP_TG_OWN"}
        )
        self.assertEqual(status, 201, body)
        saved = json.loads((Path(self.tmp.name) / "channels.json").read_text(encoding="utf-8"))["channels"]
        self.assertEqual(saved[-1]["token_env"], "AIP_TG_OWN")

    def test_broken_file_is_503_and_problem(self):
        (Path(self.tmp.name) / "channels.json").write_text("{не json", encoding="utf-8")
        status, body = self.json("POST", "/api/channels/channels", {"label": "Мой", "chat_id": "@my_chan"})
        self.assertEqual(status, 503, body)
        self.assertIn("channels.json", body["error"])
        self.assertEqual(self.json("POST", "/api/channels/channels/x/delete")[0], 503)
        self.assertEqual(self.json("POST", "/api/channels/channels/import")[0], 503)
        _, state = self.json("GET", "/api/channels/state")
        self.assertTrue(state["error"])
        _, cfg = self.json("GET", "/api/config")
        self.assertTrue(any(p.startswith("Каналы:") for p in cfg["problems"]), cfg["problems"])

    def test_bot_check_and_subscribers(self):
        _, bot = self.json("POST", "/api/channels/bot/check")
        self.assertEqual(bot["username"], "demo_narrator_bot")
        _, subs = self.json("POST", "/api/channels/subscribers/sync")
        self.assertEqual((subs["active"], len(subs["subscribers"])), (2, 3))

    def test_get_subscribers_never_syncs(self):
        # GET с ?sync=1 (например <img src> с чужого сайта) не трогает getUpdates (SEC-008).
        narrator = self.app.modules["channels"].narrator
        calls = []
        real = type(narrator).bridge

        def spy(this, *args):
            calls.append(args)
            return real(this, *args)

        with mock.patch.object(type(narrator), "bridge", spy):
            status, _ = self.json("GET", "/api/channels/subscribers?sync=1")
            self.assertEqual(status, 200)
            self.assertEqual(calls, [("subscribers",)])
            status, _ = self.json("POST", "/api/channels/subscribers/sync")
            self.assertEqual(status, 200)
            self.assertEqual(calls[-1], ("subscribers", "--sync"))

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


class ChannelStoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.path = self.dir / "channels.json"

    def test_parallel_adds_keep_both(self):
        """Два add параллельно: без замка оба читают старый список и один канал теряется."""
        real_save = publish.save_channels
        both_read = threading.Barrier(2, timeout=0.5)

        def slow_save(path, channels):
            # Без замка оба потока дойдут сюда со старым списком; под замком второй ждёт снаружи.
            with contextlib.suppress(threading.BrokenBarrierError):
                both_read.wait()
            real_save(path, channels)

        def add(chat):
            ChannelStore(self.path).add({"label": chat, "chat_id": chat})

        with mock.patch.object(publish, "save_channels", slow_save):
            threads = [threading.Thread(target=add, args=(c,)) for c in ("@first_chan", "@second_chan")]
            for t in threads:
                t.start()
            for t in threads:
                t.join(5)
        chats = {c.chat_id for c in ChannelStore(self.path).load()}
        self.assertTrue({"@first_chan", "@second_chan"} <= chats, chats)

    def test_broken_file(self):
        self.path.write_text("[1]", encoding="utf-8")
        with self.assertRaises(ChannelsBroken):
            ChannelStore(self.path).load()
        with self.assertRaises(ChannelsBroken):
            ChannelStore(self.path).add({"label": "x", "chat_id": "@x_chan"})

    def test_only_store_saves(self):
        """publish.save_channels в коде зовёт только ChannelStore."""
        root = Path(__file__).resolve().parent.parent / "aiplatform"
        callers = sorted(
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if "save_channels(" in p.read_text(encoding="utf-8") and p.name != "publish.py"
        )
        self.assertEqual(callers, ["channels/store.py"])


if __name__ == "__main__":
    unittest.main()
