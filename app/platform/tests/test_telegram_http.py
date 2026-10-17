"""Публикация и уведомления через настоящий HTTP к фейковому Bot API (QA-007).

Запрос собирает и шлёт боевой код (publish.multipart, publish._http_post, notify._telegram_post),
фейк (tests/fake_telegram.py) разбирает multipart и отвечает по сценарию: поля, тред, подпись,
429 с retry_after, 5xx и 413 проверяются на проводе, а не подменой функции.
"""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.core.notify import Notifier
from aiplatform.studio import publish
from aiplatform.studio.llm import StudioError
from tests.fake_telegram import FakeTelegram, parse_body
from tests.fixtures import TOKEN, png
from tests.helpers import JournalCase

LONG_POST = " ".join(f"Предложение номер {n} о брокерах сообщений и очередях." for n in range(60))


class TelegramCase(unittest.TestCase):
    def setUp(self):
        self.tg = FakeTelegram().start()
        self.addCleanup(self.tg.stop)
        for patcher in (
            mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN, **self.tg.env()}),
            mock.patch.object(publish, "_sleep", self.sleeps.append),  # паузы повторов — мгновенно, но записаны
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        self.dir = folder
        self.video = folder / "video.mp4"
        self.video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 64)
        self.poster = folder / "poster.png"
        self.poster.write_bytes(png(64, 36))
        self.audio = folder / "voice.mp3"
        self.audio.write_bytes(b"ID3\x04" + b"\x00" * 64)

    @property
    def sleeps(self):
        if not hasattr(self, "_sleeps"):
            self._sleeps = []
        return self._sleeps

    def channel(self, chat_id="-1003755616451_3"):
        return publish.Channel("tg", "TG", publish.TELEGRAM, chat_id=chat_id, token_env="AIP_TG_TEST")

    def send_video(self, post="Пост о **брокерах**.", **kw):
        return publish.publish(
            self.channel(**kw), video=self.video, title="Брокеры", post=post, out_dir=self.dir, width=1920, height=1080,
            seconds=61.4,
        )  # fmt: skip


class SendFieldsTest(TelegramCase):
    def test_video_multipart_fields_and_thread(self):
        result = self.send_video()
        self.assertEqual((result["ok"], result["message_id"]), (True, 1))
        (call,) = self.tg.sent("sendVideo")
        self.assertEqual(call.token, TOKEN)
        self.assertEqual((call.chat, call.fields["message_thread_id"]), ("-1003755616451", "3"))
        self.assertEqual(
            {k: call.fields[k] for k in ("parse_mode", "supports_streaming", "width", "height", "duration")},
            {"parse_mode": "HTML", "supports_streaming": "true", "width": "1920", "height": "1080", "duration": "61"},
        )
        self.assertIn("<b>брокерах</b>", call.fields["caption"])
        name, ctype, data = call.files["video"]
        self.assertEqual((name, ctype, data), ("video.mp4", "video/mp4", self.video.read_bytes()))

    def test_channel_without_thread_sends_no_thread_field(self):
        self.send_video(chat_id="@my_channel")
        (call,) = self.tg.sent("sendVideo")
        self.assertEqual(call.chat, "@my_channel")
        self.assertNotIn("message_thread_id", call.fields)

    def test_long_caption_is_cut_to_telegram_limit(self):
        self.send_video(post=LONG_POST)
        caption = self.tg.sent("sendVideo")[0].fields["caption"]
        self.assertLessEqual(publish.visible_len(caption), publish.CAPTION_LIMIT)
        self.assertTrue(caption.startswith("Предложение номер 0"))

    def test_photo_then_audio(self):
        result = publish.publish_photo(
            self.channel(), poster=self.poster, audio=self.audio, title="Брокеры", post="Пост.", seconds=12.6
        )
        self.assertTrue(result["ok"])
        photo, audio = self.tg.sent()
        self.assertEqual((photo.method, audio.method), ("sendPhoto", "sendAudio"))
        self.assertEqual(photo.files["photo"][:2], ("poster.png", "image/png"))
        self.assertEqual(audio.files["audio"][:2], ("voice.mp3", "audio/mpeg"))
        self.assertEqual((audio.fields["title"], audio.fields["duration"]), ("Брокеры", "13"))
        self.assertEqual({photo.fields["message_thread_id"], audio.fields["message_thread_id"]}, {"3"})

    def test_chat_methods(self):
        self.tg.members = 321
        self.assertEqual(publish.members(self.channel()), 321)
        self.assertEqual(self.tg.methods(), ["getChatMemberCount"])


class ErrorsTest(TelegramCase):
    def test_429_waits_retry_after_then_sends(self):
        self.tg.fail("sendVideo", "429", retry_after=7)
        self.assertTrue(self.send_video()["ok"])
        self.assertEqual(self.sleeps, [7], "пауза — сколько попросил Telegram")
        self.assertEqual([c.accepted for c in self.tg.sent("sendVideo")], [False, True])

    def test_429_too_long_is_an_error_with_text(self):
        self.tg.fail("sendVideo", "429", retry_after=publish.MAX_RETRY_AFTER_S + 1, text="Too Many Requests: slow down")
        with self.assertRaisesRegex(StudioError, "TG: Telegram ответил «Too Many Requests: slow down»"):
            self.send_video()
        self.assertEqual(len(self.tg.sent("sendVideo")), 1, "дольше лимита не ждём и не повторяем")

    def test_5xx_on_send_is_not_retried(self):
        # Повтор отправки после 5xx мог бы задвоить пост: ошибка с текстом, решает человек.
        self.tg.fail("sendVideo", "500", text="Internal Server Error: try later")
        with self.assertRaisesRegex(StudioError, "Internal Server Error: try later"):
            self.send_video()
        self.assertEqual(len(self.tg.sent("sendVideo")), 1)

    def test_5xx_on_chat_method_is_retried(self):
        self.tg.fail("getChatMemberCount", "502", times=2)
        self.assertEqual(publish.members(self.channel()), 10)
        self.assertEqual(self.tg.methods(), ["getChatMemberCount"] * 3)

    def test_413_text(self):
        self.tg.fail("sendVideo", "413")
        with self.assertRaisesRegex(StudioError, "Request Entity Too Large"):
            self.send_video()

    def test_dropped_answer_is_unknown(self):
        self.tg.fail("sendVideo", "drop")
        with self.assertRaises(publish.Unknown):
            self.send_video()
        self.assertEqual(len(self.tg.sent("sendVideo")), 1, "после обрыва ответа повтора нет — пост мог уйти")

    def test_token_not_leaked_into_error(self):
        self.tg.fail("sendVideo", "refuse", text=f"Unauthorized: token {TOKEN} revoked")
        with self.assertRaises(StudioError) as caught:
            self.send_video()
        self.assertNotIn(TOKEN, str(caught.exception))


class StepFailureTest(JournalCase):
    """5xx и 429 через студию: шаг публикации падает с текстом Telegram в журнале."""

    def test_5xx_fails_publish_step_with_text(self):
        pid = self.video_project()
        self.tg.fail("sendVideo", "500", chat="-1001111111111", text="Internal Server Error: overloaded")
        failed = self.press(pid, channels=["tg-a"])
        self.assertEqual(self.statuses(failed)["publish"], "failed", failed["log"])
        self.assertTrue(any("Internal Server Error: overloaded" in line for line in failed["log"]), failed["log"])

    def test_429_fails_step_when_telegram_asks_to_wait_too_long(self):
        pid = self.video_project()
        self.tg.fail("sendVideo", "429", times=0, retry_after=publish.MAX_RETRY_AFTER_S + 60)
        failed = self.press(pid, channels=["tg-a"])
        self.assertEqual(self.statuses(failed)["publish"], "failed", failed["log"])
        self.assertTrue(any("Too Many Requests" in line for line in failed["log"]), failed["log"])


class NotifyTest(unittest.TestCase):
    def test_owner_message_goes_over_http(self):
        with FakeTelegram() as tg, mock.patch.dict(os.environ, tg.env()), tempfile.TemporaryDirectory() as tmp:
            notifier = Notifier(Path(tmp), fake=True, telegram=lambda: (TOKEN, {"chat_id": "42"}))
            results = notifier.send("Выпуск упал", "Озвучка: нет сети", show=True)
        self.assertEqual(results[0]["ok"], True, results)
        (call,) = tg.sent("sendMessage")
        self.assertEqual((call.token, call.chat), (TOKEN, "42"))
        self.assertIn("Выпуск упал", call.fields["text"])


class ParseBodyTest(unittest.TestCase):
    def test_multipart_from_publish(self):
        body, ctype = publish.multipart({"chat_id": "1", "caption": "Привет"}, {})
        self.assertEqual(parse_body(ctype, body), ({"chat_id": "1", "caption": "Привет"}, {}))


if __name__ == "__main__":
    unittest.main()
