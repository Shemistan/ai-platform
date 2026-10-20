"""Статья через LLM: текст для озвучки по правилам tts-guide, код — на экран, а не в озвучку (PRD-029)."""

import json
import unittest

from aiplatform.studio import prompts, slides
from tests.fixtures import NEEDS_FFMPEG
from tests.helpers import StudioCase

CODE_ARTICLE = """Как читать план запроса

Команда показывает план. Подробнее: https://example.com/docs

```sql
EXPLAIN SELECT * FROM tenk1;
```

План — это дерево. Читайте его снизу вверх.
"""


class SplitScreenTest(unittest.TestCase):
    def test_mark_anchors_sentence_and_is_removed(self):
        marked = "# Первый раздел статьи\n\nРаз. Два. [экран 1] На экране запрос.\n\nТретий абзац."
        code = {"title": "Запрос", "language": "sql", "code": "SELECT 1;"}
        text, screen = slides.split_screen(marked, [code])
        self.assertNotIn("экран 1", text)
        self.assertEqual(text.split("\n\n")[1], "Раз. Два. На экране запрос.")
        self.assertTrue(text.startswith("# Первый раздел"))  # заголовок для пауз озвучки остаётся
        self.assertEqual(len(screen), 1)
        self.assertEqual((screen[0]["para"], screen[0]["sent"], screen[0]["kind"]), (1, 2, "code"))
        self.assertEqual(screen[0]["code"], "SELECT 1;")

    def test_mark_alone_goes_to_next_paragraph_and_bad_code_dropped(self):
        marked = "Начало.\n\n[экран 1]\n\nНа экране запрос.\n\n[экран 2] Пусто."
        items = [{"title": "", "code": "SELECT 1;"}, {"title": "пусто", "code": "   "}]
        text, screen = slides.split_screen(marked, items)
        self.assertEqual(slides.paragraphs(text), ["Начало.", "На экране запрос.", "Пусто."])
        self.assertEqual([(s["para"], s["sent"], s["title"]) for s in screen], [(1, 0, "Код")])

    def test_code_without_mark_goes_to_last_paragraph(self):
        _, screen = slides.split_screen("Раз.\n\nДва.", [{"title": "К", "code": "x = 1"}])
        self.assertEqual((screen[0]["para"], screen[0]["sent"]), (1, 0))

    def test_code_slide_replaces_model_slide_on_its_sentence(self):
        paras = ["Раз. Два.", "Три."]
        plan = slides.parse_plan([{"para": 0, "title": "a"}, {"para": 0, "sent": 1, "title": "b"}], paras)
        screen = [{"para": 0, "sent": 1, "kind": "code", "title": "Код", "code": "x = 1", "language": "go"}]
        merged = slides.with_screen(plan, screen, paras)
        self.assertEqual([(s.para, s.sent, s.kind) for s in merged], [(0, 0, "bullets"), (0, 1, "code")])
        self.assertEqual(merged[1].data["code"], "x = 1")

    def test_plan_prompt_names_taken_sentences(self):
        screen = [{"para": 2, "sent": 1, "title": "Запрос"}]
        self.assertIn("[2] (1) — Запрос", prompts.plan(["a"], "T", 1, screen=screen))
        self.assertNotIn("уже будет слайд с кодом", prompts.plan(["a"], "T", 1))

    def test_narration_prompt_carries_rules(self):
        prompt = prompts.narration("текст", "T")
        self.assertTrue(prompt.startswith(prompts.TASK + prompts.NARRATION))
        self.assertIn(prompts.TTS_RULES, prompt)
        self.assertIn(prompts.DATA_NOTE, prompt)


class AdaptFlowTest(StudioCase):
    def adapted(self, settings: dict, text: str = CODE_ARTICLE) -> tuple[dict, object]:
        project = self.create(scenario="article", text=text, settings={"review_text": True} | settings, run=True)
        return self.settle(project["id"]), self.folder(project["id"])

    def test_code_leaves_narration_and_goes_to_screen(self):
        project, folder = self.adapted({})
        self.assertEqual(self.statuses(project)["text"], "waiting", project["log"])
        text = (folder / "text.md").read_text(encoding="utf-8")
        self.assertNotIn("```", text)
        self.assertNotIn("EXPLAIN", text)
        self.assertNotIn("https://", text)
        self.assertIn("На экране", text)
        self.assertIn("```sql", (folder / "source.md").read_text(encoding="utf-8"))  # исходник цел
        screen = json.loads((folder / "screen.json").read_text(encoding="utf-8"))
        self.assertEqual([s["code"] for s in screen], ["EXPLAIN SELECT * FROM tenk1;"])
        self.assertEqual(project["artifacts"]["text"], text)

    def test_switched_off_text_goes_as_is(self):
        project, folder = self.adapted({"adapt_text": False})
        self.assertIn("```sql", (folder / "text.md").read_text(encoding="utf-8"))
        self.assertFalse((folder / "screen.json").exists())
        self.assertEqual(self.statuses(project)["text"], "waiting", project["log"])

    def test_edit_before_run_edits_source(self):
        project = self.create(scenario="article", text=CODE_ARTICLE, settings={"review_text": True})
        status, _ = self.json("POST", f"/api/studio/projects/{project['id']}/artifacts/text", {"text": "Новый текст."})
        self.assertEqual(status, 200)
        folder = self.folder(project["id"])
        self.assertEqual((folder / "source.md").read_text(encoding="utf-8"), "Новый текст.\n")
        self.assertEqual(self.statuses(self.json("GET", f"/api/studio/projects/{project['id']}")[1])["text"], "pending")


@NEEDS_FFMPEG
class AdaptRenderTest(StudioCase):
    def test_plan_shows_code_on_its_sentence(self):
        project = self.create(scenario="article", text=CODE_ARTICLE, settings={"draft": True}, run=True)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        folder = self.folder(project["id"])
        screen = json.loads((folder / "screen.json").read_text(encoding="utf-8"))
        plan = json.loads((folder / "plan.json").read_text(encoding="utf-8"))["slides"]
        code = [s for s in plan if s["kind"] == "code"]
        self.assertEqual(
            [(s["para"], s.get("sent", 0), s["code"]) for s in code],
            [(screen[0]["para"], screen[0]["sent"], screen[0]["code"])],
        )


if __name__ == "__main__":
    unittest.main()
