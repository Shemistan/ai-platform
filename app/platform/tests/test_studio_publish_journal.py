"""Публикация без дублей: журнал published.json по каждому каналу и части поста (BE-001, QA-002).

Сбой второго канала, сбой sendAudio после sendPhoto, рестарт посреди публикации, таймаут
ответа и «Запустить всё» после падения — повтор не шлёт пост туда, куда он уже ушёл.
"""

import io
import json
import os
import shutil
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from aiplatform.studio import publish
from aiplatform.studio.llm import StudioError
from tests.test_studio_channels import TOKEN
from tests.test_studio_flow import ARTICLE, StudioCase, png_bytes

CHATS = {"tg-a": "-1001111111111", "tg-b": "-1002222222222"}


def chat_of(body: bytes) -> str:
    for cid, chat in CHATS.items():
        if f'name="chat_id"\r\n\r\n{chat}\r\n'.encode() in body:
            return cid
    return "?"


class FakeTelegram:
    """Подмена publish._http_post: пишет вызовы, по флагам отвечает отказом или «рвёт» ответ."""

    def __init__(self):
        self.calls: list[tuple[str, str, bool]] = []  # (канал, метод, принят ли пост)
        self.refuse: set[tuple[str, str]] = set()  # Telegram ответил ok=false
        self.drop: set[tuple[str, str]] = set()  # ответа нет: обрыв после отправки

    def __call__(self, url, body, ctype):
        method = url.rsplit("/", 1)[-1]
        if method in ("getChatMemberCount", "getChat"):
            return {"ok": True, "result": 10}
        key = (chat_of(body), method)
        if key in self.drop:
            self.calls.append((*key, False))
            raise ConnectionResetError("обрыв посреди ответа")
        if key in self.refuse:
            self.calls.append((*key, False))
            return {"ok": False, "description": "Too Many Requests: retry after 5"}
        self.calls.append((*key, True))
        return {"ok": True, "result": {"message_id": len(self.calls)}}

    def count(self, channel, method, *, accepted=None):
        return sum(
            1 for c, m, ok in self.calls if c == channel and m == method and (accepted is None or ok == accepted)
        )


class JournalCase(StudioCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        self.channels_file = folder / "channels.json"
        self.channels_file.write_text(
            json.dumps(
                [
                    {"id": cid, "label": cid.upper(), "type": "telegram", "chat_id": chat, "token_env": "AIP_TG_TEST"}
                    for cid, chat in CHATS.items()
                ]
            ),
            encoding="utf-8",
        )
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(self.channels_file)}
        super().setUp()
        self.tg = FakeTelegram()
        for patcher in (
            mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}),
            mock.patch.object(publish, "_http_post", self.tg),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def video_project(self) -> str:
        pid = self.create(scenario="article", text=ARTICLE)["id"]
        (self.folder(pid) / "video.mp4").write_bytes(b"x")
        return pid

    def press(self, pid, **body):
        status, view = self.json("POST", f"/api/studio/projects/{pid}/publish", body)
        self.assertEqual(status, 200, view)
        return self.settle(pid)

    def resolve(self, pid, channel, posted):
        status, view = self.json(
            "POST", f"/api/studio/projects/{pid}/publish/resolve", {"channel": channel, "posted": posted}
        )
        self.assertEqual(status, 200, view)
        return self.settle(pid)

    def journal(self, pid) -> dict:
        data = json.loads((self.folder(pid) / "published.json").read_text(encoding="utf-8"))
        return {r["channel"]: r for r in data}


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


class RepublishTest(JournalCase):
    """Повтор с «Опубликовать повторно» — новая запись канала, прежняя цела (AN-005)."""

    def records(self, pid) -> list[dict]:
        return json.loads((self.folder(pid) / "published.json").read_text(encoding="utf-8"))

    def test_force_keeps_first_record(self):
        pid = self.video_project()
        self.press(pid, channels=["tg-a"])
        first = self.records(pid)[0]
        snap = {"at": first["sent"] + 3600, "views": 42, "members": 10}
        # Отклик у первой публикации уже собран — повтор его не стирает.
        (self.folder(pid) / "published.json").write_text(json.dumps([first | {"stats": [snap]}]), encoding="utf-8")

        self.press(pid, channels=["tg-a"], force=True)
        records = self.records(pid)
        self.assertEqual(len(records), 2, "повтор — новая запись, не замена")
        old, new = records
        self.assertEqual((old["message_id"], old["stats"], old["state"]), (first["message_id"], [snap], "sent"))
        self.assertNotIn("skipped", old)
        self.assertEqual((old["attempt"], old["publication_id"]), (1, f"{pid}:tg-a:1"))
        self.assertEqual((new["attempt"], new["publication_id"], new["republish"]), (2, f"{pid}:tg-a:2", True))
        self.assertNotEqual(new["message_id"], old["message_id"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 2)

        # Без force повтор не шлёт: текущая попытка уже ушла.
        self.press(pid, channels=["tg-a"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 2)
        self.assertEqual(len(self.records(pid)), 2)

        board = self.json("GET", "/api/studio/board")[1]
        item = next(i for i in board["projects"] if i["id"] == pid)
        self.assertEqual([r["republish"] for r in item["published"]], [False, True], "доска: «повторно»")

    def test_failed_republish_retry_stays_second_attempt(self):
        pid = self.video_project()
        self.press(pid, channels=["tg-a"])
        self.tg.refuse.add(("tg-a", "sendVideo"))
        self.press(pid, channels=["tg-a"], force=True)
        self.tg.refuse.clear()
        self.press(pid, channels=["tg-a"])
        records = self.records(pid)
        self.assertEqual([(r["attempt"], r["state"]) for r in records], [(1, "sent"), (2, "sent")])
        self.assertTrue(records[1]["republish"])


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
