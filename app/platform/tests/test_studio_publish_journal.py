"""Публикация без дублей: журнал published.json по каждому каналу и части поста (BE-001, QA-002).

Сбой второго канала, сбой sendAudio после sendPhoto, рестарт посреди публикации, таймаут
ответа и «Запустить всё» после падения — повтор не шлёт пост туда, куда он уже ушёл.
"""

import io
import unittest
import urllib.error
from unittest import mock

from aiplatform.studio import publish
from aiplatform.studio.llm import StudioError
from tests.fixtures import ARTICLE, png_bytes
from tests.helpers import JournalCase


class PartialPublishTest(JournalCase):
    def test_second_channel_fails_retry_does_not_duplicate_first(self):
        pid = self.video_project()
        self.tg.refuse.add(("tg-b", "sendVideo"))
        failed = self.press(pid, channels=["tg-a", "tg-b"])
        self.assertEqual(self.statuses(failed)["publish"], "failed", failed["log"])
        journal = self.journal(pid)
        self.assertEqual(journal["tg-a"]["state"], "sent", "первый канал записан сразу после отправки")
        self.assertEqual(journal["tg-b"]["state"], "failed")

        self.tg.refuse.clear()
        done = self.press(pid, channels=["tg-a", "tg-b"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1, "в первый канал пост не задвоен")
        self.assertEqual(self.tg.count("tg-b", "sendVideo", accepted=True), 1)
        journal = self.journal(pid)
        self.assertEqual((journal["tg-a"]["state"], journal["tg-b"]["state"]), ("sent", "sent"))
        self.assertTrue(journal["tg-a"]["skipped"])

    def test_audio_fails_after_photo_retry_sends_only_audio(self):
        pid = self.create(scenario="article", text=ARTICLE)["id"]
        self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/settings", {"post_format": "photo"})[0], 200)
        (self.folder(pid) / "poster.png").write_bytes(png_bytes())
        (self.folder(pid) / "voice.mp3").write_bytes(b"ID3fake")
        self.tg.refuse.add(("tg-a", "sendAudio"))
        failed = self.press(pid, channels=["tg-a"])
        self.assertEqual(self.statuses(failed)["publish"], "failed", failed["log"])
        record = self.journal(pid)["tg-a"]
        self.assertEqual(record["parts"]["photo"]["state"], "sent")
        self.assertEqual(record["parts"]["audio"]["state"], "failed")

        self.tg.refuse.clear()
        done = self.press(pid, channels=["tg-a"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertEqual(self.tg.count("tg-a", "sendPhoto"), 1, "картинка не задвоена")
        self.assertEqual(self.tg.count("tg-a", "sendAudio", accepted=True), 1)
        record = self.journal(pid)["tg-a"]
        self.assertEqual(record["state"], "sent")
        self.assertEqual(record["message_id"], record["parts"]["photo"]["message_id"])

    def test_restart_mid_publish_no_duplicate(self):
        pid = self.video_project()
        self.tg.drop.add(("tg-b", "sendVideo"))
        self.press(pid, channels=["tg-a", "tg-b"])
        self.tg.drop.clear()

        from aiplatform import server as server_module

        app = server_module.App(self.config)  # сервис заново из того же каталога данных
        self.addCleanup(app.close)
        module = app.modules["studio"]
        module.publish_now(pid, {"channels": ["tg-a", "tg-b"]})
        module.studio.join(pid, 30)
        project = module.studio.get(pid)
        self.assertEqual(project["steps"]["publish"]["status"], "waiting", "статус второго канала неизвестен")
        self.assertIn("Статус неизвестен", project["steps"]["publish"]["note"])
        self.assertEqual(self.tg.count("tg-b", "sendVideo"), 1, "без решения человека повтора нет")

        module.resolve_publish(pid, {"channel": "tg-b", "posted": False})
        module.studio.join(pid, 30)
        project = module.studio.get(pid)
        self.assertEqual(project["steps"]["publish"]["status"], "done", project["log"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1)
        self.assertEqual(self.tg.count("tg-b", "sendVideo", accepted=True), 1)


class UnknownStateTest(JournalCase):
    def test_timeout_is_unknown_and_waits_for_human(self):
        pid = self.video_project()
        self.tg.drop.add(("tg-a", "sendVideo"))
        waiting = self.press(pid, channels=["tg-a"])
        self.assertEqual(self.statuses(waiting)["publish"], "waiting", waiting["log"])
        self.assertEqual(self.journal(pid)["tg-a"]["state"], "unknown")
        self.tg.drop.clear()

        again = self.press(pid, channels=["tg-a"])
        self.assertEqual(self.statuses(again)["publish"], "waiting")
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1, "повтора без решения человека нет")
        self.assertEqual(
            self.json("POST", f"/api/studio/projects/{pid}/publish/resolve", {"channel": "tg-b", "posted": True})[0],
            400,
            "у tg-b нечего решать",
        )

        done = self.resolve(pid, "tg-a", True)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1, "«Пост есть» — не шлём")
        self.assertEqual(self.journal(pid)["tg-a"]["state"], "sent")

    def test_run_all_after_failure_needs_new_confirmation(self):
        pid = self.video_project()
        module = self.app.modules["studio"]
        for step in ("text", "poster_prompt", "poster", "plan", "images", "clips", "voice", "render", "short"):
            module.studio.mark_done(pid, step)
        self.tg.refuse.add(("tg-a", "sendVideo"))
        failed = self.press(pid, channels=["tg-a"])
        self.assertEqual(self.statuses(failed)["publish"], "failed")
        self.tg.refuse.clear()

        self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/run", {})[0], 200)
        after = self.settle(pid)
        self.assertEqual(self.statuses(after)["publish"], "waiting", after["log"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1, "«Запустить всё» не публикует без подтверждения")
        state = module.studio.get(pid)["steps"]["publish"]
        self.assertFalse(state.get("approved"))
        self.assertFalse(state.get("force"))


class HttpPostTest(unittest.TestCase):
    """Сбой до отправки — «не отправлено»; без ответа — «неизвестно»; 200 с мусором — отправлено."""

    def test_classification(self):
        refused = urllib.error.URLError(ConnectionRefusedError("нет соединения"))
        with mock.patch("urllib.request.urlopen", side_effect=refused):
            with self.assertRaises(StudioError) as caught:
                publish._http_post("https://x/sendVideo", b"", "text/plain")
            self.assertNotIsInstance(caught.exception, publish.Unknown)
        for exc in (TimeoutError("read timed out"), ConnectionResetError("reset")):
            with (
                self.subTest(exc=exc),
                mock.patch("urllib.request.urlopen", side_effect=exc),
                self.assertRaises(publish.Unknown),
            ):
                publish._http_post("https://x/sendVideo", b"", "text/plain")

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        with mock.patch("urllib.request.urlopen", return_value=Resp(b"<html>oops")):
            self.assertTrue(publish._http_post("https://x/sendVideo", b"", "text/plain")["ok"])


if __name__ == "__main__":
    unittest.main()
