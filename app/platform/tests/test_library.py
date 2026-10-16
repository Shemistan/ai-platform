"""Вкладка «Статьи»: библиотека движка, озвучка, проверка, рассылка и агент — на фейковом движке."""

import unittest

from aiplatform.library.service import LibraryModule, engine_args, progress
from tests.helpers import PlatformCase


def library_only(ctx):
    return [LibraryModule(ctx)]


class LibraryUnitTest(unittest.TestCase):
    def test_progress(self):
        self.assertEqual(progress("=== 2/5 2026-10-01-x"), "Статья 2 из 5")
        self.assertEqual(progress("Очереди абзац 3/7"), "Абзац 3 из 7")
        self.assertEqual(progress("телеграм: озвучка отправлена 1 из 1"), "Отправка в Telegram")
        self.assertIsNone(progress("что-то ещё"))

    def test_engine_args(self):
        self.assertEqual(engine_args({}), [])
        args = engine_args({"engine": "espeech", "voice": "Shem", "speed": 1.1, "steps": 16})
        self.assertEqual(args, ["--engine", "espeech", "--voice", "Shem", "--speed", "1.1", "--steps", "16"])
        self.assertEqual(engine_args({"engine": "silero", "speed": 1.5}), ["--engine", "silero"])


class LibraryTest(PlatformCase):
    modules = staticmethod(library_only)
    process_env = {"FAKE_TTS_STEP_DELAY": "0.01", "FAKE_TTS_FAIL": "", "FAKE_NARRATOR_DELAY": "0.01"}

    def task(self, task):
        return self.poll(f"/api/library/tasks/{task['id']}", lambda t: t["status"] not in ("queued", "running"))

    def article(self, article_id):
        return self.json("GET", f"/api/library/articles/{article_id}")[1]

    def test_state_and_card(self):
        status, state = self.json("GET", "/api/library/state")
        self.assertEqual(status, 200, state)
        self.assertEqual(len(state["articles"]), 3)
        self.assertEqual(state["unsent"], 1)
        self.assertEqual(state["channels"], ["HISTORY_CHAN", "VOICE_NEWS"])
        voiced = next(a for a in state["articles"] if a["sent"])
        card = self.article(voiced["id"])
        self.assertIn("Совпадение по словам: 96%", card["check_report"])
        [url] = card["audio_urls"].values()
        status, headers, _ = self.call("GET", url + "?download=1")
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/mpeg"))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertNotIn("folder", card)
        self.assertEqual(self.json("GET", "/api/library/articles/nope")[0], 404)

    def test_add_by_text_prepare_synth_verify_send(self):
        payload = {"title": "Свой текст", "text": "Первый абзац.\n\nВторой абзац.", "summary": "О чём-то."}
        status, card = self.json("POST", "/api/library/articles", payload)
        self.assertEqual(status, 201, card)
        article_id = card["id"]
        self.assertTrue(card["stale"])

        _, task = self.json("POST", f"/api/library/articles/{article_id}/synth", {"engine": "silero", "voice": "baya"})
        done = self.task(task)
        self.assertEqual(done["status"], "done", done)
        card = self.article(article_id)
        self.assertTrue(card["has_audio"])
        self.assertFalse(card["sent"])

        _, task = self.json("POST", f"/api/library/articles/{article_id}/verify")
        self.assertEqual(self.task(task)["status"], "done")
        self.assertEqual(self.article(article_id)["check"], 92)

        _, task = self.json("POST", f"/api/library/articles/{article_id}/send", {"channel": "VOICE_NEWS"})
        done = self.task(task)
        self.assertIn("в VOICE_NEWS", done["title"])
        self.assertTrue(self.article(article_id)["sent"])
        self.assertEqual(self.json("POST", f"/api/library/articles/{article_id}/send", {"channel": "a b"})[0], 400)

    def test_fetch_and_agent_prepare(self):
        status, card = self.json("POST", "/api/library/articles", {"url": "https://example.dev/hello-world"})
        self.assertEqual(status, 201, card)
        self.assertFalse(card["has_script"])
        self.assertEqual(self.json("POST", "/api/library/articles", {"url": "не ссылка"})[0], 400)
        self.assertEqual(self.json("POST", "/api/library/articles", {"url": "https://example.dev/fail"})[0], 400)
        _, task = self.json("POST", f"/api/library/articles/{card['id']}/prepare")
        self.assertEqual(self.task(task)["status"], "done")
        card = self.article(card["id"])
        self.assertTrue(card["has_script"] and card["summary"])

    def test_save_and_guards(self):
        _, state = self.json("GET", "/api/library/state")
        bare = next(a for a in state["articles"] if not a["has_script"])
        self.assertEqual(self.json("POST", f"/api/library/articles/{bare['id']}/verify")[0], 400)
        self.assertEqual(self.json("POST", f"/api/library/articles/{bare['id']}", {})[0], 400)
        _, card = self.json("POST", f"/api/library/articles/{bare['id']}", {"script": "Текст.", "summary": "Кратко."})
        self.assertTrue(card["has_script"])
        self.assertEqual(self.json("POST", f"/api/library/articles/{bare['id']}/nope")[0], 404)

    def test_render_and_unsent(self):
        _, task = self.json("POST", "/api/library/render", {"engine": "silero"})
        self.assertEqual(self.task(task)["status"], "done")
        _, state = self.json("GET", "/api/library/state")
        self.assertFalse(any(a["stale"] for a in state["articles"] if a["has_script"]))
        _, task = self.json("POST", "/api/library/unsent/mark")
        self.assertEqual(self.task(task)["status"], "done")
        self.assertEqual(self.json("GET", "/api/library/state")[1]["unsent"], 0)


if __name__ == "__main__":
    unittest.main()
