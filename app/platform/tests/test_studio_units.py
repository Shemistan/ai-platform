"""Сценарии по частям: план слайдов, тайминг, ответы LLM, новости, публикация."""

import json
import os
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from aiplatform.fakes import fake_llm
from aiplatform.studio import llm, news, pipeline, prompts, publish, slides
from aiplatform.studio.llm import StudioError

TEXT = "Первый абзац.\n\n# Заголовок\n\nВторой абзац из пяти слов.\n\nТретий."


class SlidesTest(unittest.TestCase):
    def test_paragraphs(self):
        self.assertEqual(
            slides.paragraphs(TEXT), ["Первый абзац.", "Заголовок", "Второй абзац из пяти слов.", "Третий."]
        )
        self.assertEqual(
            slides.paragraphs("одна строка без пустых строк\nи вторая"), ["одна строка без пустых строк и вторая"]
        )

    def test_parse_plan_clamps_and_orders(self):
        raw = {
            "slides": [
                {"para": 7, "title": "Конец", "bullets": ["а", "", "б", "в", "г"]},
                {"para": "2", "title": "Середина"},
                {"para": 2, "title": "Дубль — отброшен"},
                {"para": 1, "title": ""},
                "мусор",
                {"para": 1, "title": "Начало", "image_prompt": "x"},
            ]
        }
        plan = slides.parse_plan(raw, 4)
        self.assertEqual([(s.para, s.title) for s in plan], [(0, "Начало"), (2, "Середина"), (3, "Конец")])
        self.assertEqual(plan[2].bullets, ["а", "б", "в"])

    def test_parse_plan_errors(self):
        for raw in ({"slides": []}, {"slides": [{"title": ""}]}, {"nope": 1}, "текст"):
            with self.subTest(raw), self.assertRaises(StudioError):
                slides.parse_plan(raw, 3)
        # Список без обёртки тоже годится; один абзац — один слайд с нуля.
        self.assertEqual(slides.parse_plan([{"para": 5, "title": "x"}], 1)[0].para, 0)

    def test_durations_cover_audio(self):
        paras = ["раз два три четыре", "пять шесть", "семь восемь", "девять десять"]
        plan = [slides.Slide(0, "a"), slides.Slide(1, "b"), slides.Slide(3, "c")]
        d = slides.durations(paras, plan, 20.0, intro_s=2.0)
        self.assertAlmostEqual(sum(d) + 2.0, 20.0, places=2)
        self.assertEqual(len(d), 3)
        self.assertTrue(all(x >= slides.MIN_SLIDE_S for x in d), d)

    def test_durations_short_audio_and_crowded_slides(self):
        paras = ["а"] * 10
        plan = [slides.Slide(i, str(i)) for i in range(10)]
        d = slides.durations(paras, plan, 6.0)
        self.assertAlmostEqual(sum(d), 6.0, places=2)
        self.assertTrue(all(x > 0 for x in d), d)
        # Слайды на последних абзацах не вылезают за конец аудио.
        late = [slides.Slide(0, "a"), slides.Slide(9, "b")]
        self.assertAlmostEqual(sum(slides.durations(paras, late, 10.0)), 10.0, places=2)


class LlmTest(unittest.TestCase):
    def test_extract_json(self):
        self.assertEqual(llm.extract_json('Вот:\n```json\n{"a": 1}\n```\nготово'), {"a": 1})
        self.assertEqual(llm.extract_json('бла {"b": [1, 2]} бла'), {"b": [1, 2]})
        self.assertEqual(llm.extract_json("[1, 2]"), [1, 2])
        with self.assertRaises(StudioError):
            llm.extract_json("нет json")

    def test_plain(self):
        self.assertEqual(llm.plain('```\n"a cat on a roof"\n```'), "a cat on a roof")

    def test_complete_via_cli(self):
        import sys

        cmd = (sys.executable, "-c", "import sys; print(sys.stdin.read().upper())")
        self.assertEqual(llm.complete("привет", cmd=cmd, cancel=threading.Event()), "ПРИВЕТ")
        bad = (sys.executable, "-c", "import sys; print('boom'); sys.exit(2)")
        with self.assertRaisesRegex(StudioError, "boom"):
            llm.complete("x", cmd=bad, cancel=threading.Event())

    def test_fake_llm_follows_prompts(self):
        paras = slides.paragraphs(TEXT)
        data = llm.extract_json(fake_llm.answer(prompts.plan(paras, "T", 4)))
        self.assertEqual(len(slides.parse_plan(data, len(paras))), 4)
        short = llm.extract_json(fake_llm.answer(prompts.short(TEXT, "T", 100)))
        self.assertGreaterEqual(len(short["beats"]), 2)
        self.assertIn("no text", fake_llm.answer(prompts.poster(TEXT, "T")))


class NewsTest(unittest.TestCase):
    def test_parse_day(self):
        today = date(2026, 10, 15)
        self.assertEqual(news.parse_day("", today), date(2026, 10, 14))
        self.assertEqual(news.parse_day("2026-10-01", today), date(2026, 10, 1))
        for bad in ("2026-10-16", "15.10.2026", 5):
            with self.subTest(bad), self.assertRaises(ValueError):
                news.parse_day(bad, today)

    def test_parse_where(self):
        lines = [
            "Судья:    готово — /lib/review.md",
            "Озвучить: /Volumes/My Disk/lib/2026/10/14/digest/narration.md → mp3 в /x/",
        ]
        self.assertEqual(news.parse_where(lines, "2026-10-14"), Path("/Volumes/My Disk/lib/2026/10/14/digest"))
        with self.assertRaisesRegex(StudioError, "нет"):
            news.parse_where(["Дайджеста за 2026-10-14 нет"], "2026-10-14")
        with self.assertRaisesRegex(StudioError, "не сказал"):
            news.parse_where(["что-то другое"], "2026-10-14")


class PipelineTest(unittest.TestCase):
    def test_dependents(self):
        self.assertEqual(pipeline.dependents("poster"), ["render", "short", "publish"])
        self.assertEqual(pipeline.dependents("text")[:3], ["poster_prompt", "poster", "plan"])
        self.assertEqual(pipeline.dependents("publish"), [])


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.video = self.dir / "video.mp4"
        self.video.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 100)

    def test_default_and_custom_channels(self):
        defaults = publish.load_channels(self.dir / "missing.json")
        self.assertEqual([c.type for c in defaults], ["manual", "manual"])
        path = self.dir / "channels.json"
        path.write_text(
            json.dumps([{"id": "tg", "label": "TG", "type": "telegram", "chat_id": "@x", "token_env": "AIP_TEST_TG"}]),
            encoding="utf-8",
        )
        (tg,) = publish.load_channels(path)
        self.assertEqual(tg.problem(), "нет токена в переменной AIP_TEST_TG")
        path.write_text(json.dumps([{"id": "Плохой", "type": "x"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            publish.load_channels(path)

    def test_caption(self):
        self.assertEqual(
            publish.post_html("**Жирно** <тег> [ссылка](https://e.x/a?b=1&c=2)"),
            '<b>Жирно</b> &lt;тег&gt; <a href="https://e.x/a?b=1&amp;c=2">ссылка</a>',
        )
        self.assertEqual(publish.caption("x" * 2000, "Заголовок"), "<b>Заголовок</b>")
        self.assertEqual(publish.plain_text("**a** [b](https://c)"), "a b (https://c)")

    def test_manual_package(self):
        channel = publish.Channel("yt", "YouTube", publish.MANUAL)
        result = publish.publish(channel, video=self.video, title="Т", post="**П**", out_dir=self.dir / "publish")
        self.assertTrue(result["ok"])
        self.assertEqual((self.dir / "publish/yt/description.txt").read_text(encoding="utf-8"), "П\n")
        self.assertTrue((self.dir / "publish/yt/video.mp4").is_file())

    def test_telegram_send_and_secret_hygiene(self):
        channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="@news", token_env="AIP_TEST_TG")
        with self.assertRaisesRegex(StudioError, "нет токена"):
            publish.publish(channel, video=self.video, title="Т", post="П", out_dir=self.dir)
        sent = {}

        def fake_post(url, body, ctype):
            sent.update(url=url, body=body, ctype=ctype)
            return {"ok": True, "result": {"message_id": 42}}

        with mock.patch.dict(os.environ, {"AIP_TEST_TG": "123:SECRET"}):
            result = publish.publish(
                channel,
                video=self.video,
                title="Т",
                post="**П**",
                out_dir=self.dir,
                http_post=fake_post,
                width=1920,
                height=1080,
            )
            self.assertIn("42", result["message"])
            self.assertTrue(sent["url"].endswith("/bot123:SECRET/sendVideo"))
            self.assertIn(b'name="chat_id"\r\n\r\n@news', sent["body"])
            self.assertIn(b"<b>\xd0\x9f</b>", sent["body"])
            self.assertIn(b'name="width"\r\n\r\n1920', sent["body"])

            def failing(url, body, ctype):
                return {"ok": False, "description": "Unauthorized for 123:SECRET"}

            with self.assertRaises(StudioError) as err:
                publish.publish(channel, video=self.video, title="Т", post="П", out_dir=self.dir, http_post=failing)
            self.assertNotIn("SECRET", str(err.exception))


if __name__ == "__main__":
    unittest.main()
