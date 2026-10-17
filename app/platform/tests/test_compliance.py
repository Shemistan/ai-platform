"""Пометки реестра и источник в публикации: подпись, кадр постера, аудио, подтверждение (CMP-*)."""

import html
import json
import os
import shutil
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from aiplatform.fakes import fake_narrate
from aiplatform.studio import publish
from aiplatform.studio.llm import StudioError
from aiplatform.studio.render import VERTICAL, Renderer
from aiplatform.updates import registry
from tests.test_studio_flow import NEEDS_FFMPEG, StudioCase, png_bytes

DEFAULT = registry.Registry(registry.DEFAULT_FILE).entries()
META_NOTE = (
    "Компания Meta (продукты Facebook и Instagram) признана экстремистской организацией и запрещена на территории РФ."
)


class _Tags(HTMLParser):
    """Проверка, что теги подписи закрыты и вложены правильно."""

    def __init__(self):
        super().__init__()
        self.stack: list[str] = []
        self.broken = False

    def handle_starttag(self, tag, attrs):
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.broken = True


def assert_valid_html(case: unittest.TestCase, body: str) -> None:
    parser = _Tags()
    parser.feed(body)
    parser.close()
    case.assertFalse(parser.broken or parser.stack, f"оборванные теги: {body[-200:]}")


def long_post(chars: int) -> str:
    sentence = "Компания Meta показала **новую модель** и [рассказала](https://example.dev/a?b=1&c=2) о планах. "
    return (sentence * (chars // len(sentence) + 1))[:chars].rsplit(" ", 1)[0]


class CaptionTailTest(unittest.TestCase):
    def test_long_post_keeps_footnote(self):
        post = registry.with_footnotes(long_post(1600), "", DEFAULT)
        self.assertGreater(publish.visible_len(publish.post_html(post)), publish.CAPTION_LIMIT)
        body = publish.caption(post, "Заголовок")
        self.assertLessEqual(publish.visible_len(body), publish.CAPTION_LIMIT)
        self.assertIn(html.escape(META_NOTE, quote=False), body)
        self.assertNotEqual(body, "<b>Заголовок</b>")
        self.assertTrue(body.startswith("Компания Meta"), "тело поста не заменено заголовком")
        assert_valid_html(self, body)

    def test_cut_never_breaks_markup(self):
        for size in range(1500, 2400, 13):
            post = registry.with_footnotes(long_post(size), "", DEFAULT) + "\nИсточник: [Автор](https://example.dev/x)"
            body = publish.caption(post, "Заголовок")
            self.assertLessEqual(publish.visible_len(body), publish.CAPTION_LIMIT, size)
            self.assertIn(html.escape(META_NOTE, quote=False), body)
            self.assertTrue(body.endswith('Источник: <a href="https://example.dev/x">Автор</a>'), body[-120:])
            assert_valid_html(self, body)

    def test_short_post_untouched(self):
        post = registry.with_footnotes("Meta выпустила **очки**.", "", DEFAULT)
        self.assertEqual(publish.caption(post, "Т"), publish.post_html(post))

    def test_never_silently_title_only(self):
        body = publish.caption("x" * 2000, "Заголовок")
        self.assertTrue(body.startswith("xxx") and body.endswith("…"))
        self.assertLessEqual(publish.visible_len(body), publish.CAPTION_LIMIT)

    def test_tail_over_limit_is_error(self):
        tail = "\n".join(f"* {META_NOTE} {n}" for n in range(12))
        with self.assertRaises(StudioError) as err:
            publish.caption(f"Пост про Meta.\n\n{tail}", "Заголовок")
        self.assertIn("сноски", str(err.exception))

    def test_split_tail(self):
        post = "Абзац.\n\n* Пометка.\n\nИсточник: автор"
        self.assertEqual(publish.split_tail(post), ("Абзац.", "* Пометка.\n\nИсточник: автор"))
        self.assertEqual(publish.split_tail("**Жирный** абзац."), ("**Жирный** абзац.", ""))


TOKEN = "123456:" + "A" * 35
META_POST = "**Очки Meta**\n\nКомпания Meta показала очки — [подробнее](https://example.dev/q)"


class StillNotesTest(unittest.TestCase):
    def test_still_burns_notes(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        calls = []
        renderer = Renderer(ffmpeg="ffmpeg", font="")
        with mock.patch.object(Renderer, "_run", lambda self, argv, cancel, what: calls.append(argv)):
            renderer.poster_clip(
                folder / "p.png", folder / "still.mp4", 3.0, VERTICAL, threading.Event(), notes=[META_NOTE]
            )
            renderer.poster_clip(folder / "p.png", folder / "bare.mp4", 3.0, VERTICAL, threading.Event())
        graph, bare = (argv[argv.index("-filter_complex") + 1] for argv in calls)
        note = folder / "still-note.txt"
        self.assertNotIn("drawtext", bare)
        self.assertIn(str(note), graph)
        self.assertIn("drawtext", graph)
        self.assertIn("Meta", note.read_text(encoding="utf-8"))

    def test_audio_caption(self):
        post = registry.with_footnotes(META_POST, "", DEFAULT)
        body = publish.audio_caption(post, "Очки Meta")
        self.assertTrue(body.startswith("<b>Очки Meta</b>"))
        self.assertIn(html.escape(META_NOTE, quote=False), body)
        self.assertEqual(publish.audio_caption("Про Go.", "Go"), "<b>Go</b>")


@NEEDS_FFMPEG
class PostNotesFlowTest(StudioCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        channels = [{"id": "tg", "label": "TG", "type": "telegram", "chat_id": "-1001", "token_env": "AIP_TEST_TG"}]
        (folder / "channels.json").write_text(json.dumps(channels), encoding="utf-8")
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(folder / "channels.json")}
        super().setUp()

    def upload(self, pid):
        mp3 = Path(self.tmp.name) / "v.mp3"
        fake_narrate.write_mp3(mp3, 2.0)
        self.call("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())
        self.call("POST", f"/api/studio/projects/{pid}/upload/voice", mp3.read_bytes())

    def test_poster_video_has_note_in_frame(self):
        project = self.create(scenario="post", post=META_POST, settings={"branding": False}, run=True)
        pid = project["id"]
        self.settle(pid)
        self.upload(pid)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        note = self.folder(pid) / ".video-clips" / "still-note.txt"
        self.assertIn("Meta", note.read_text(encoding="utf-8"))

    def test_photo_post_audio_caption_has_footnote(self):
        settings = {"post_format": "photo", "branding": False}
        project = self.create(scenario="post", post=META_POST, settings=settings)
        pid = project["id"]
        self.upload(pid)
        calls = []

        def fake_post(url, body, ctype):
            calls.append((url.rsplit("/", 1)[-1], body))
            return {"ok": True, "result": {"message_id": len(calls)}}

        with mock.patch.dict(os.environ, {"AIP_TEST_TG": TOKEN}), mock.patch.object(publish, "_http_post", fake_post):
            self.json("POST", f"/api/studio/projects/{pid}/run", {})
            self.settle(pid)
            self.json("POST", f"/api/studio/projects/{pid}/publish", {"channels": ["tg"]})
            done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        audio = next(body for method, body in calls if method == "sendAudio")
        self.assertIn(b'name="caption"', audio)
        self.assertIn(META_NOTE.encode(), audio)


if __name__ == "__main__":
    unittest.main()
