"""Пометки реестра и источник в публикации: подпись, кадр постера, аудио, подтверждение (CMP-*)."""

import html
import unittest
from html.parser import HTMLParser

from aiplatform.studio import publish
from aiplatform.studio.llm import StudioError
from aiplatform.updates import registry

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


if __name__ == "__main__":
    unittest.main()
