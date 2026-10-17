"""Ревью и публикация выпуска из Telegram-бота (PRD-001).

Превью владельцу, когда публикация ждёт человека; «Опубликовать» — с подтверждением и тем же
путём, что кнопка студии, ровно один раз; чужой chat id ничего не делает; ответ — новая подпись.
Telegram владельца — DemoTelegram (демо-режим), каналы публикации — FakeTelegram журнала BE-001.
"""

import json
import time
import unittest

from aiplatform.channels.service import BotServe
from aiplatform.studio import review as review_module
from aiplatform.studio import steps
from tests.test_studio_flow import ARTICLE
from tests.test_studio_publish_journal import JournalCase

OWNER = 42


def click(pid: str, nonce: str, action: str, user: int = OWNER) -> dict:
    return {"callback_query": {"id": "q", "from": {"id": user}, "data": f"rv:{action}:{pid}:{nonce}"}}


class ReviewCase(JournalCase):
    def setUp(self):
        super().setUp()
        status, body = self.json("POST", "/api/settings/section/notify", {"telegram_chat": str(OWNER)})
        self.assertEqual(status, 200, body)
        self.studio = self.app.modules["studio"]
        self.review = self.studio.review
        self.tg_owner = self.review.transport  # демо: Telegram владельца в памяти
        self.assertIsInstance(self.tg_owner, review_module.DemoTelegram)

    def waiting(self) -> str:
        """Выпуск с готовым видео и выбранными каналами, публикация ждёт кнопки."""
        settings = {"channels": ["tg-a", "tg-b"]}
        pid = self.create(scenario="article", text=ARTICLE, settings=settings)["id"]
        (self.folder(pid) / "video.mp4").write_bytes(b"x")
        (self.folder(pid) / steps.POST).write_text("**Главное** о сборщике мусора.\n", encoding="utf-8")
        status, view = self.json("POST", f"/api/studio/projects/{pid}/run", {"step": "publish"})
        self.assertEqual(status, 200, view)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "waiting", done["log"])
        deadline = time.time() + 10  # превью шлёт поток выпуска сразу после остановки
        while self.review.record(pid) is None and time.time() < deadline:
            time.sleep(0.05)
        self.assertIsNotNone(self.review.record(pid), self.tg_owner.calls)
        return pid

    def keyboard(self, call) -> list[str]:
        markup = json.loads(call[1]["reply_markup"])
        return [b["text"] for row in markup["inline_keyboard"] for b in row]

    def last(self, method: str):
        return [c for c in self.tg_owner.calls if c[0] == method][-1]


class PreviewTest(ReviewCase):
    def test_waiting_publish_sends_video_preview_with_three_buttons(self):
        self.waiting()
        methods = self.tg_owner.methods()
        self.assertEqual(methods[:2], ["sendVideo", "sendMessage"], self.tg_owner.calls)
        video = self.tg_owner.calls[0]
        self.assertEqual(video[1]["chat_id"], str(OWNER))
        self.assertEqual(video[2], {"video": "video.mp4"})
        self.assertIn("<b>Главное</b>", video[1]["caption"])
        panel = self.tg_owner.calls[1]
        self.assertEqual(self.keyboard(panel), ["Опубликовать в 2 канала", "Поправить подпись", "В архив"])
        self.assertIn("TG-A, TG-B", panel[1]["text"])
        self.assertEqual(self.tg.calls, [], "в каналы ничего не ушло")

    def test_without_owner_chat_no_preview(self):
        self.json("POST", "/api/settings/section/notify", {"telegram_chat": ""})
        pid = self.create(scenario="article", text=ARTICLE, settings={"channels": ["tg-a"]})["id"]
        (self.folder(pid) / "video.mp4").write_bytes(b"x")
        self.json("POST", f"/api/studio/projects/{pid}/run", {"step": "publish"})
        self.settle(pid)
        time.sleep(0.2)
        self.assertEqual(self.tg_owner.calls, [])

    def test_big_video_falls_back_to_poster(self):
        pid = self.create(scenario="article", text=ARTICLE, settings={"channels": ["tg-a"]})["id"]
        folder = self.folder(pid)
        with open(folder / "video.mp4", "wb") as fh:
            fh.truncate(review_module.publish.BOT_VIDEO_LIMIT + 1)
        (folder / "poster.png").write_bytes(b"png")
        found = self.review._media({"settings": {}}, folder)
        self.assertEqual(found[0], "sendPhoto")
        self.assertIn("больше 50 МБ", found[3])


class ButtonsTest(ReviewCase):
    def test_publish_needs_confirmation_and_goes_out_exactly_once(self):
        pid = self.waiting()
        nonce = self.review.record(pid)["nonce"]
        self.review.handle(click(pid, nonce, "pub"))
        confirm = self.last("editMessageText")
        self.assertIn("TG-A", confirm[1]["text"])
        self.assertEqual(self.keyboard(confirm), ["Да, опубликовать", "Отмена"])
        self.assertEqual(self.tg.calls, [], "до «Да» ничего не публикуется")

        self.review.handle(click(pid, nonce, "ok"))
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1)
        self.assertEqual(self.tg.count("tg-b", "sendVideo"), 1)
        self.assertEqual({r["state"] for r in self.journal(pid).values()}, {"sent"})

        self.review.handle(click(pid, nonce, "ok"))  # двойное нажатие
        self.review.handle(click(pid, nonce, "pub"))
        time.sleep(0.2)
        self.settle(pid)
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1, "повторное «Да» не шлёт дубль")
        self.assertEqual(self.tg.count("tg-b", "sendVideo"), 1)
        answers = [c[1].get("text", "") for c in self.tg_owner.calls if c[0] == "answerCallbackQuery"]
        self.assertTrue(any("Уже" in a for a in answers), answers)

    def test_foreign_chat_does_nothing(self):
        pid = self.waiting()
        nonce = self.review.record(pid)["nonce"]
        before = list(self.tg_owner.calls)
        for action in ("pub", "ok", "arch", "edit"):
            self.review.handle(click(pid, nonce, action, user=777))
        reply = {"message_id": 9, "from": {"id": 777}, "text": "взлом", "reply_to_message": {"message_id": 1001}}
        self.review.handle({"message": reply})
        time.sleep(0.2)
        self.assertEqual(self.tg_owner.calls, before, "чужому ни ответа, ни действия")
        view = self.settle(pid)
        self.assertEqual(self.statuses(view)["publish"], "waiting")
        self.assertFalse(view.get("archived"))
        self.assertEqual(self.tg.calls, [])
        self.assertNotIn("взлом", (self.folder(pid) / steps.POST).read_text(encoding="utf-8"))

    def test_stale_preview_buttons_do_nothing(self):
        pid = self.waiting()
        self.review.handle(click(pid, "deadbeef", "ok"))
        time.sleep(0.2)
        self.assertEqual(self.tg.calls, [])
        self.assertIn("устарело", self.last("answerCallbackQuery")[1]["text"])

    def test_cancel_restores_buttons(self):
        pid = self.waiting()
        nonce = self.review.record(pid)["nonce"]
        self.review.handle(click(pid, nonce, "pub"))
        self.review.handle(click(pid, nonce, "no"))
        self.assertEqual(self.keyboard(self.last("editMessageText"))[0], "Опубликовать в 2 канала")
        self.assertEqual(self.tg.calls, [])

    def test_archive_button(self):
        pid = self.waiting()
        nonce = self.review.record(pid)["nonce"]
        self.review.handle(click(pid, nonce, "arch"))
        _, view = self.json("GET", f"/api/studio/projects/{pid}")
        self.assertTrue(view["archived"])
        self.assertIsNone(self.review.record(pid))


class CaptionTest(ReviewCase):
    def test_reply_changes_post_before_publish(self):
        pid = self.waiting()
        rec = self.review.record(pid)
        self.review.handle(click(pid, rec["nonce"], "edit"))
        prompt = self.last("sendMessage")
        self.assertEqual(json.loads(prompt[1]["reply_markup"]), {"force_reply": True})
        prompt_id = self.review.record(pid)["prompt_id"]
        msg = {"message_id": 77, "from": {"id": OWNER}, "text": "Новая подпись выпуска"}
        self.review.handle({"message": msg | {"reply_to_message": {"message_id": prompt_id}}})
        self.assertEqual((self.folder(pid) / steps.POST).read_text(encoding="utf-8"), "Новая подпись выпуска\n")
        self.assertIn("Новая подпись выпуска", self.last("editMessageCaption")[1]["caption"])

        self.review.handle(click(pid, rec["nonce"], "ok"))
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1)

    def test_reply_to_preview_video_also_works(self):
        pid = self.waiting()
        rec = self.review.record(pid)
        msg = {"message_id": 78, "from": {"id": OWNER}, "text": "Подпись ответом на видео"}
        self.review.handle({"message": msg | {"reply_to_message": {"message_id": rec["media_id"]}}})
        self.assertIn("Подпись ответом на видео", (self.folder(pid) / steps.POST).read_text(encoding="utf-8"))


class OwnerTest(unittest.TestCase):
    def test_owner_by_id_or_username(self):
        self.assertTrue(review_module.is_owner({"id": 42}, "42"))
        self.assertFalse(review_module.is_owner({"id": 43}, "42"))
        self.assertTrue(review_module.is_owner({"id": 1, "username": "Shem"}, "@shem"))
        self.assertFalse(review_module.is_owner({"id": 42}, ""))

    def test_plural(self):
        self.assertEqual(
            [review_module.plural(n) for n in (1, 2, 5, 11, 21)], ["канал", "канала", "каналов", "каналов", "канал"]
        )


class BotServeUpdatesTest(unittest.TestCase):
    def test_update_lines_go_to_handler_not_log(self):
        serve = BotServe(narrator=None)
        got = []
        serve.on_update = got.append
        serve._dispatch('{"update_id": 1, "callback_query": {"id": "q"}}')
        serve._dispatch("не json")  # сбой разбора — в лог процесса, не исключение
        self.assertEqual(got, [{"update_id": 1, "callback_query": {"id": "q"}}])
