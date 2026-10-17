"""Повторы запросов к Bot API из сценариев (BE-012): 429 ждём, неизвестный исход не повторяем."""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.studio import publish
from aiplatform.studio.llm import StudioError

TOKEN = "123456:" + "A" * 35
TOO_MANY = {"ok": False, "error_code": 429, "description": "Too Many Requests", "parameters": {"retry_after": 3}}


class Replies:
    """http_post с ответами по очереди; исключение в очереди — бросается."""

    def __init__(self, *queue):
        self.queue = list(queue)
        self.methods: list[str] = []

    def __call__(self, url, body, ctype):
        self.methods.append(url.rsplit("/", 1)[-1])
        reply = self.queue.pop(0) if self.queue else {"ok": True, "result": {"message_id": 1}}
        if isinstance(reply, Exception):
            raise reply
        return reply


class RetriesTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)
        self.video = self.dir / "video.mp4"
        self.video.write_bytes(b"x")
        self.channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="@my_chan", token_env="AIP_TEST_TG")
        self.sleeps: list[float] = []
        for patch in (
            mock.patch.dict("os.environ", {"AIP_TEST_TG": TOKEN}),
            mock.patch.object(publish, "_sleep", self.sleeps.append),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def publish(self, http_post):
        return publish.publish(
            self.channel, video=self.video, title="T", post="Пост", out_dir=self.dir, http_post=http_post
        )

    def test_429_waits_retry_after_then_sends(self):
        replies = Replies(TOO_MANY, {"ok": True, "result": {"message_id": 9}})
        self.assertEqual(self.publish(replies)["message_id"], 9)
        self.assertEqual(replies.methods, ["sendVideo", "sendVideo"])
        self.assertIn(3, self.sleeps)

    def test_send_video_timeout_is_not_retried(self):
        replies = Replies(publish.Unknown("Telegram не ответил"))
        with self.assertRaises(publish.Unknown):
            self.publish(replies)
        self.assertEqual(replies.methods, ["sendVideo"])

    def test_send_retried_when_not_sent(self):
        replies = Replies(StudioError("Telegram недоступен: URLError"))
        self.assertTrue(self.publish(replies)["ok"])
        self.assertEqual(replies.methods, ["sendVideo", "sendVideo"])

    def test_send_5xx_is_not_retried(self):
        replies = Replies({"ok": False, "error_code": 502, "description": "Bad Gateway"})
        with self.assertRaisesRegex(StudioError, "Bad Gateway"):
            self.publish(replies)
        self.assertEqual(replies.methods, ["sendVideo"])

    def test_chat_calls_retried_on_5xx_and_timeout(self):
        replies = Replies(
            {"ok": False, "error_code": 502, "description": "Bad Gateway"},
            publish.Unknown("нет ответа"),
            {"ok": True, "result": 77},
        )
        self.assertEqual(publish.members(self.channel, replies), 77)
        self.assertEqual(len(replies.methods), publish.ATTEMPTS)
        down = Replies(*[publish.Unknown("нет ответа")] * 5)
        self.assertIsNone(publish.members(self.channel, down), "сбой отклика публикацию не держит")
        self.assertEqual(len(down.methods), publish.ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
