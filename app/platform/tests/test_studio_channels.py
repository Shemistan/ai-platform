"""Каналы Telegram как у narrator: telegram_tems.md, темы в группе, токен, кнопка «Опубликовать»."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.studio import publish
from aiplatform.studio.llm import StudioError
from tests.test_studio_flow import ARTICLE, StudioCase

TOKEN = "123456:" + "A" * 35


class NarratorChannelsTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_import_from_telegram_tems(self):
        (self.dir / "telegram_tems.md").write_text(
            "# каналы\nVOICE_NEWS=-1003755616451_3\n\nHISTORY-CHAN=-1002629071990\nNICK=@my_channel\n", encoding="utf-8"
        )
        channels = publish.load_channels(self.dir / "channels.json", self.dir)
        tg = {c.id: c for c in channels if c.type == publish.TELEGRAM}
        self.assertEqual(list(tg), ["tg-voice-news", "tg-history-chan", "tg-nick"])
        self.assertEqual(tg["tg-voice-news"].label, "Telegram: VOICE_NEWS")
        self.assertEqual(tg["tg-voice-news"].target(), {"chat_id": "-1003755616451", "message_thread_id": "3"})
        self.assertEqual(tg["tg-history-chan"].target(), {"chat_id": "-1002629071990"})
        self.assertEqual(tg["tg-nick"].target(), {"chat_id": "@my_channel"})
        self.assertEqual(
            publish.Channel("x", "x", publish.TELEGRAM, chat_id="-1001234567_1").target(), {"chat_id": "-1001234567"}
        )
        self.assertEqual(len(channels), 5, "плюс два пакета для ручной загрузки")

    def test_bad_line_is_an_error(self):
        (self.dir / "telegram_tems.md").write_text("VOICE_NEWS=куда-то\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "telegram_tems.md:1"):
            publish.load_channels(self.dir / "channels.json", self.dir)

    def test_token_like_narrator(self):
        channel = publish.load_channels(self.dir / "c.json", self.dir)[0]
        tg = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="-1002629071990", token_file=channel.token_file)
        with mock.patch.dict(os.environ, {}, clear=False):
            for key in (publish.NARRATOR_TOKEN_ENV, publish.NARRATOR_TOKEN_FILE_ENV):
                os.environ.pop(key, None)
            self.assertIn("bot_token.md", tg.problem())
            (self.dir / "bot_token.md").write_text(f"токен: {TOKEN}\n", encoding="utf-8")
            self.assertEqual(tg.token(), TOKEN)
            self.assertIsNone(tg.problem())
            os.environ[publish.NARRATOR_TOKEN_ENV] = "999999:" + "B" * 35
            self.assertTrue(tg.token().startswith("999999:"))
        self.assertNotIn("token_file", tg.public())

    def test_new_channel_and_storage(self):
        existing = publish.load_channels(self.dir / "c.json")
        channel = publish.new_channel(existing, " Мой  канал ", "-1003755616451_3", "landscape")
        self.assertEqual((channel.id, channel.label), ("tg-1003755616451-3", "Мой канал"))
        twin = publish.new_channel([*existing, channel], "Шортсы", "-1003755616451_3", "vertical")
        self.assertEqual(twin.id, "tg-1003755616451-3-2")
        for args, message in (
            (("", "@chan_name", "landscape"), "Назови"),
            (("X", "chan", "landscape"), "id канала"),
            (("X", "@chan_name", "square"), "Формат"),
            (("Y", "-1003755616451_3", "landscape"), "уже есть"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                publish.new_channel([*existing, channel], *args)
        publish.save_channels(self.dir / "c.json", [*existing, channel])
        again = publish.load_channels(self.dir / "c.json")
        self.assertEqual([c.id for c in again], [*[c.id for c in existing], channel.id])
        self.assertEqual(json.loads((self.dir / "c.json").read_text())[-1]["chat_id"], "-1003755616451_3")

    def test_topic_goes_as_message_thread_id(self):
        video = self.dir / "v.mp4"
        video.write_bytes(b"x")
        channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="-1003755616451_14", token_env="AIP_TG_TEST")
        sent = {}

        def fake_post(url, body, ctype):
            if url.endswith("/sendVideo"):
                sent["body"] = body
            return {"ok": True, "result": {"message_id": 7}}

        with mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}):
            publish.publish(channel, video=video, title="Т", post="П", out_dir=self.dir, http_post=fake_post)
        self.assertIn(b'name="chat_id"\r\n\r\n-1003755616451\r\n', sent["body"])
        self.assertIn(b'name="message_thread_id"\r\n\r\n14\r\n', sent["body"])

    def test_token_env_only_allowed_names_and_token_like(self):
        # SEC-006: чужая переменная или не-токен — ни одного запроса в api.telegram.org.
        video = self.dir / "v.mp4"
        video.write_bytes(b"x")
        audio, poster = self.dir / "a.mp3", self.dir / "p.jpg"
        audio.write_bytes(b"x")
        poster.write_bytes(b"x")
        calls = []

        def fake_post(url, body, ctype):
            calls.append(url)
            return {"ok": True, "result": 5}

        cases = [
            ("HF_TOKEN", TOKEN, "AIP_TG_"),
            ("AIP_TG_TEST", "hf_" + "x" * 40, "не похож на токен"),
            ("AIP_TG_TEST", "", "нет токена в переменной AIP_TG_TEST"),
        ]
        for env, value, problem in cases:
            with self.subTest(env=env, value=value):
                channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="@x", token_env=env)
                with mock.patch.dict(os.environ, {env: value}):
                    self.assertIn(problem, channel.problem())
                    self.assertEqual(channel.token(), "")
                    post = {"title": "Т", "post": "П", "http_post": fake_post}
                    with self.assertRaises(StudioError):
                        publish.publish(channel, video=video, out_dir=self.dir, **post)
                    with self.assertRaises(StudioError):
                        publish.publish_photo(channel, poster=poster, audio=audio, **post)
                    self.assertIsNone(publish.members(channel, http_post=fake_post))
        self.assertEqual(calls, [])
        for name in ("AIP_TG_NEWS", "AIP_TELEGRAM_TOKEN", "NARRATOR_TELEGRAM_TOKEN", "NARRATOR_TELEGRAM_TOKEN_2"):
            self.assertTrue(publish.token_env_allowed(name), name)
        for name in ("HF_TOKEN", "ANTHROPIC_API_KEY", "NARRATOR_TELEGRAM_TOKEN_FILE", "AIP_TG_", "PATH"):
            self.assertFalse(publish.token_env_allowed(name), name)


class PublishButtonTest(StudioCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        self.channels_file = folder / "channels.json"
        self.channels_file.write_text(
            json.dumps(
                [
                    {
                        "id": "tg",
                        "label": "TG",
                        "type": "telegram",
                        "chat_id": "-1003755616451_3",
                        "token_env": "AIP_TG_TEST",
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(self.channels_file)}
        super().setUp()

    def test_publish_button_and_repeat(self):
        project = self.create(scenario="article", text=ARTICLE)
        pid = project["id"]
        (self.folder(pid) / "video.mp4").write_bytes(b"x")
        calls = []

        def fake_post(url, body, ctype):
            if url.endswith("/getChatMemberCount"):  # подписчики в момент публикации — для отклика
                return {"ok": True, "result": 120}
            calls.append(body)
            return {"ok": True, "result": {"message_id": len(calls)}}

        def press(**body):
            status, view = self.json("POST", f"/api/studio/projects/{pid}/publish", body)
            self.assertEqual(status, 200, view)
            return self.settle(pid)

        with mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}), mock.patch.object(publish, "_http_post", fake_post):
            done = press(channels=["tg"])
            self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
            self.assertEqual(done["settings"]["channels"], ["tg"])
            self.assertEqual(len(calls), 1)
            self.assertTrue(done["published"][0]["sent"])
            record = done["published"][0]
            self.assertEqual(
                (record["message_id"], record["chat_id"], record["members_at_publish"]), (1, "-1003755616451", 120)
            )

            again = press(channels=["tg"])
            self.assertEqual(len(calls), 1, "уже опубликовано — без «повторно» не шлём")
            self.assertTrue(again["published"][0]["skipped"])
            self.assertIn("Опубликовать повторно", again["published"][0]["message"])

            forced = press(channels=["tg"], force=True)
            self.assertEqual(len(calls), 2)
            self.assertNotIn("skipped", forced["published"][0])

        for body in ({"channels": []}, {"channels": ["nope"]}, {"channels": ["tg"], "force": "да"}):
            self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/publish", body)[0], 400, body)

    def test_add_and_delete_channel(self):
        status, body = self.json(
            "POST", "/api/studio/channels", {"label": "Шортсы", "chat_id": "@my_shorts", "format": "vertical"}
        )
        self.assertEqual(status, 201, body)
        self.assertEqual(body["added"], "tg-my-shorts")
        saved = json.loads(self.channels_file.read_text(encoding="utf-8"))
        self.assertEqual([c["id"] for c in saved], ["tg", "tg-my-shorts"])
        self.assertEqual(saved[1]["format"], "vertical")
        _, cfg = self.json("GET", "/api/studio/config")
        self.assertIn("tg-my-shorts", [c["id"] for c in cfg["channels"]])
        self.assertEqual(self.json("POST", "/api/studio/channels", {"label": "X", "chat_id": "плохой"})[0], 400)
        status, body = self.json("POST", "/api/studio/channels/tg-my-shorts/delete")
        self.assertEqual((status, [c["id"] for c in body["channels"]]), (200, ["tg"]))
        self.assertEqual(self.json("POST", "/api/studio/channels/tg-my-shorts/delete")[0], 404)


if __name__ == "__main__":
    unittest.main()
