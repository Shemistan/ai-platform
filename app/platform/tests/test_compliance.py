"""Пометки реестра и источник в публикации: подпись, кадр постера, аудио, подтверждение (CMP-*)."""

import html
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from unittest import mock

from aiplatform.fakes import fake_narrate
from aiplatform.studio import publish, series, steps
from aiplatform.studio.llm import StudioError
from aiplatform.studio.render import VERTICAL, Renderer
from aiplatform.updates import registry
from tests.fixtures import ARTICLE, NEEDS_FFMPEG, SOURCE, png_bytes
from tests.helpers import StudioCase

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
        channels = [{"id": "tg", "label": "TG", "type": "telegram", "chat_id": "-1001", "token_env": "AIP_TG_TEST"}]
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

        with mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}), mock.patch.object(publish, "_http_post", fake_post):
            self.json("POST", f"/api/studio/projects/{pid}/run", {})
            self.settle(pid)
            self.json("POST", f"/api/studio/projects/{pid}/publish", {"channels": ["tg"]})
            done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        audio = next(body for method, body in calls if method == "sendAudio")
        self.assertIn(b'name="caption"', audio)
        self.assertIn(META_NOTE.encode(), audio)


@NEEDS_FFMPEG
class DisclaimersOffTest(StudioCase):
    def ready_post(self, post):
        settings = {"disclaimers": False, "branding": False}
        project = self.create(scenario="post", post=post, settings=settings, run=True)
        pid = project["id"]
        self.settle(pid)
        PostNotesFlowTest.upload(self, pid)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        return pid

    def publish(self, pid, **extra):
        status, view = self.json("POST", f"/api/studio/projects/{pid}/publish", {"channels": ["download-16x9"]} | extra)
        self.assertEqual(status, 202, view)
        done = self.settle(pid)
        return done, next(s for s in done["steps"] if s["id"] == "publish")

    def test_mention_without_notes_needs_reason(self):
        pid = self.ready_post(META_POST)
        done, step = self.publish(pid)
        self.assertEqual(step["status"], "failed", done["log"])
        self.assertIn("упоминания из реестра (Meta), пометки выключены", step["error"])
        self.assertFalse((self.folder(pid) / "published.json").exists())

        done, step = self.publish(pid, ignore_disclaimers="  новость о судебном решении,   согласовано  ")
        self.assertEqual(step["status"], "done", done["log"])
        (record,) = steps.load_published(self.folder(pid))
        off = record["disclaimers_off"]
        self.assertEqual(off["reason"], "новость о судебном решении, согласовано")
        self.assertEqual(off["mentions"], ["Meta"])
        self.assertGreater(off["at"], 0)

    def test_no_mentions_publish_freely(self):
        pid = self.ready_post("**Про Go**\n\nСборщик мусора стал быстрее.")
        done, step = self.publish(pid)
        self.assertEqual(step["status"], "done", done["log"])
        (record,) = steps.load_published(self.folder(pid))
        self.assertNotIn("disclaimers_off", record)

    def test_reason_must_be_text(self):
        pid = self.create(scenario="post", post=META_POST)["id"]
        status, _ = self.json(
            "POST", f"/api/studio/projects/{pid}/publish", {"channels": ["download-16x9"], "ignore_disclaimers": 1}
        )
        self.assertEqual(status, 400)


def ffprobe_tags(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return {k.lower(): v for k, v in json.loads(out.stdout).get("format", {}).get("tags", {}).items()}


class AiLabelTest(unittest.TestCase):
    """CMP-011: строка об ИИ в неудаляемом хвосте, плашка «ИИ» и метка в файлах."""

    def test_ai_line_by_generators(self):
        self.assertEqual(publish.ai_line(), "")
        line = publish.ai_line(voice=True, images=True)
        self.assertEqual(line, "ИИ-контент: озвучено ИИ-голосом; иллюстрации сгенерированы нейросетью.")
        post = publish.with_ai_line(registry.with_footnotes(long_post(1600), "", DEFAULT), line)
        self.assertEqual(publish.with_ai_line(post, line), post)
        body = publish.caption(post, "Заголовок")
        self.assertLessEqual(publish.visible_len(body), publish.CAPTION_LIMIT)
        self.assertTrue(body.endswith(line), body[-200:])
        self.assertIn(html.escape(META_NOTE, quote=False), body)
        self.assertIn(line, publish.audio_caption(post, "Заголовок"))

    def test_badge_and_metadata_in_ffmpeg_args(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        calls = []

        def run(self, argv, cancel, what):
            calls.append(argv)
            Path(argv[-1]).touch()  # склейка переименовывает .part.mp4

        with mock.patch.object(Renderer, "_run", run):
            Renderer(ffmpeg="ffmpeg", font="", ai_badge=True).poster_clip(
                folder / "p.png", folder / "a.mp4", 3.0, VERTICAL, threading.Event(), notes=[META_NOTE]
            )
            Renderer(ffmpeg="ffmpeg", font="").poster_clip(
                folder / "p.png", folder / "b.mp4", 3.0, VERTICAL, threading.Event()
            )
            Renderer(ffmpeg="ffmpeg", font="").concat(
                [folder / "a.mp4"], folder / "v.mp3", folder / "o.mp4", 3.0, threading.Event()
            )
        badged, bare, concat = calls
        self.assertIn("text='ИИ'", badged[badged.index("-filter_complex") + 1])
        self.assertNotIn("ИИ", bare[bare.index("-filter_complex") + 1])
        self.assertIn("comment=AI-generated", concat)
        self.assertIn("AI_GENERATED=1", concat)


@NEEDS_FFMPEG
class AiLabelFlowTest(StudioCase):
    def test_published_package_is_labeled(self):
        settings = {"channels": ["download-16x9"], "auto_publish": True}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        folder = self.folder(project["id"])
        description = (folder / "publish" / "download-16x9" / "description.txt").read_text(encoding="utf-8")
        self.assertIn("ИИ-контент: озвучено ИИ-голосом; иллюстрации сгенерированы нейросетью.", description)
        self.assertIn(publish.SYNTHETIC_FLAG, description)
        self.assertEqual(ffprobe_tags(folder / "video.mp4").get("comment"), "AI-generated")
        self.assertEqual(ffprobe_tags(folder / "voice.mp3").get("ai_generated"), "1")


@NEEDS_FFMPEG
class RegistryFreshnessTest(StudioCase):
    def test_stale_registry_warns_before_and_at_publish(self):
        # CMP-013: реестр сверяли давно — предупреждение до запуска и в состоянии шага публикации.
        old = (date.today() - timedelta(days=40)).isoformat()
        registry.Registry(Path(self.tmp.name) / registry.FILE_NAME).save(DEFAULT, old)
        pid = DisclaimersOffTest.ready_post(self, "**Про Go**\n\nСборщик мусора стал быстрее.")
        project = self.json("GET", f"/api/studio/projects/{pid}")[1]
        self.assertFalse(any("Реестр" in x["text"] for x in project["preflight"]), "без каналов о реестре молчим")
        done, step = DisclaimersOffTest.publish(self, pid)
        self.assertEqual(step["status"], "done", done["log"])
        self.assertIn("40 дн. назад", step["registry_warning"])
        self.assertTrue(any("40 дн. назад" in x["text"] and x["level"] == "warn" for x in done["preflight"]))

        registry.Registry(Path(self.tmp.name) / registry.FILE_NAME).save(DEFAULT)  # сверили сегодня
        done, step = DisclaimersOffTest.publish(self, pid, force=True)
        self.assertIsNone(step["registry_warning"])
        self.assertFalse(any("Реестр" in x["text"] for x in done["preflight"]))


@NEEDS_FFMPEG
class ArticleSourceTest(StudioCase):
    SETTINGS = {"channels": ["download-16x9"], "auto_publish": True}

    def test_article_without_source_is_not_published(self):
        payload = {"scenario": "article", "text": ARTICLE, "settings": self.SETTINGS, "run": True}
        status, project = self.json("POST", "/api/studio/projects", payload)
        self.assertEqual(status, 201, project)
        done = self.settle(project["id"])
        step = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(step["status"], "failed", done["log"])
        self.assertIn("только с источником", step["error"])
        self.assertFalse((self.folder(project["id"]) / "publish").exists())

    def test_source_in_caption_and_last_slide(self):
        project = self.create(scenario="article", text=ARTICLE, settings=self.SETTINGS, run=True)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        folder = self.folder(project["id"])
        description = (folder / "publish" / "download-16x9" / "description.txt").read_text(encoding="utf-8")
        self.assertIn(f"Источник: {SOURCE['source_author']} — оригинал ({SOURCE['source_url']})", description)
        notes = sorted((folder / ".video-clips").glob("*/note.txt"))
        self.assertTrue(notes, "у последнего слайда есть полоса с источником")
        self.assertIn(SOURCE["source_url"], notes[-1].read_text(encoding="utf-8").replace("\n", ""))
        caption = publish.caption((folder / "post.md").read_text(encoding="utf-8") + "\n\nИсточник: x", "T")
        self.assertTrue(caption.endswith("Источник: x"))

    def test_bad_source_url(self):
        settings = self.SETTINGS | {"source_url": "javascript:alert(1)"}
        payload = {"scenario": "article", "text": ARTICLE, "settings": settings}
        status, _ = self.json("POST", "/api/studio/projects", payload)
        self.assertEqual(status, 400)


@NEEDS_FFMPEG
class SeriesLicenseTest(StudioCase):
    """CMP-021: серия частями по чужой статье без права на переработку в канал не уходит."""

    TEXT = (
        "# Начало\n\nСборщик мусора стал быстрее. Паузы короче вдвое. Память освобождается раньше.\n\n"
        "# Конец\n\nИтог простой. Обновляйтесь на новую версию. Тесты проходят быстрее."
    )

    def part(self, settings):
        source = self.create(scenario="article", text=self.TEXT, title="Чужая", settings=settings)
        split = series.split  # короткие части — короткий рендер
        with mock.patch.object(series, "split", lambda text: split(text, 10)):
            status, body = self.json("POST", f"/api/studio/projects/{source['id']}/split", {})
        self.assertEqual(status, 201, body)
        return body["projects"][0]["id"]

    def test_series_without_license_is_not_published(self):
        pid = self.part({"channels": ["download-16x9"], "auto_publish": True})
        project = self.json("GET", f"/api/studio/projects/{pid}")[1]
        self.assertEqual(project["settings"]["source_license"], "none")  # поле есть у выпуска и шага «Публикация»
        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        done = self.settle(pid)
        step = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(step["status"], "failed", done["log"])
        self.assertIn("права на переработку нет", step["error"])
        self.assertFalse((self.folder(pid) / "publish").exists())

        status, _ = self.json("POST", f"/api/studio/projects/{pid}/settings", {"source_license": "own"})
        self.assertEqual(status, 200)
        status, view = self.json("POST", f"/api/studio/projects/{pid}/publish", {"channels": ["download-16x9"]})
        self.assertEqual(status, 202, view)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])

    def test_bad_license(self):
        payload = {"scenario": "article", "text": ARTICLE, "settings": {"source_license": "maybe"}}
        self.assertEqual(self.json("POST", "/api/studio/projects", payload)[0], 400)

    def test_source_editable_in_publish_step(self):
        static = Path(publish.__file__).parents[1] / "static" / "studio"
        js = (static / "publish.js").read_text(encoding="utf-8")
        self.assertIn("sourceBox(p),", js)  # тело шага «Публикация»
        self.assertIn("f.source_url(), f.source_author(), f.source_license()", js)
        self.assertIn('readSettings("step-source", names)', js)
        settings = (static / "settings.js").read_text(encoding="utf-8")
        self.assertIn('SOURCE_SETTINGS = ["source_url", "source_author", "source_license"]', settings)


@NEEDS_FFMPEG
class NcVoicePublishTest(StudioCase):
    """CMP-018: озвучку движком с некоммерческой лицензией (Silero) опубликовать нельзя."""

    SETTINGS = {"channels": ["download-16x9"], "auto_publish": True, "draft": True}

    def test_silero_voice_is_not_published(self):
        project = self.create(scenario="article", text=ARTICLE, settings=self.SETTINGS | {"engine": "silero"}, run=True)
        done = self.settle(project["id"])
        step = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(step["status"], "failed", done["log"])
        self.assertIn("Silero — его лицензия некоммерческая", step["error"])
        self.assertIn("Выбери другой движок", step["error"])
        self.assertFalse((self.folder(project["id"]) / "publish").exists())
        self.assertEqual(next(s for s in done["steps"] if s["id"] == "voice")["engine"], "silero")

        status, _ = self.json("POST", f"/api/studio/projects/{project['id']}/settings", {"engine": "higgs"})
        self.assertEqual(status, 200)
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {})
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])

    def test_other_engine_is_published(self):
        project = self.create(scenario="article", text=ARTICLE, settings=self.SETTINGS | {"engine": "higgs"}, run=True)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])

    def test_engine_flag(self):
        from aiplatform.audio import voices

        # Silero — CC BY-NC, Fish — fish-audio-research (ADR 0010); Higgs — Creator Use Grant, не NC.
        self.assertEqual(sorted(e.id for e in voices.ENGINES.values() if e.license_nc), ["fish", "silero"])
        self.assertTrue(voices.ENGINES["silero"].public()["license_nc"])


class RknThresholdTest(StudioCase):
    """CMP-019: канал с 9 000+ подписчиков — предупреждение о подаче сведений в РКН."""

    def test_big_channel_warns_in_api(self):
        from aiplatform.studio import service as studio_service
        from aiplatform.studio import stats

        self.assertIsNone(stats.rkn_warning("К", 8999))
        self.assertIn("близко к 10 000", stats.rkn_warning("К", 9000))
        self.assertIn("больше 10 000", stats.rkn_warning("К", 12000))
        with mock.patch.object(studio_service, "AUDIENCE_TTL_S", 0):
            pid = self.create(scenario="post", post="Пост", settings={"channels": ["download-16x9"]})["id"]
            record = {"channel": "download-16x9", "state": "sent", "sent": 1.0e9, "members_at_publish": 9500}
            (self.folder(pid) / "published.json").write_text(json.dumps([record]), encoding="utf-8")
            channels = {c["id"]: c for c in self.json("GET", "/api/channels/state")[1]["channels"]}
            self.assertEqual(channels["download-16x9"]["members"], 9500)
            self.assertIn("9 500 подписчиков", channels["download-16x9"]["audience_warning"])
            self.assertIn("149-ФЗ", channels["download-16x9"]["audience_warning"])
            others = [c for k, c in channels.items() if k != "download-16x9"]
            self.assertTrue(all(c["audience_warning"] is None for c in others))
            project = self.json("GET", f"/api/studio/projects/{pid}")[1]
            warns = [x["text"] for x in project["preflight"] if x["level"] == "warn"]
            self.assertTrue(any("9 500 подписчиков" in x for x in warns), warns)


@NEEDS_FFMPEG
class AdEridTest(StudioCase):
    """CMP-019: реклама без erid не публикуется; с erid — «Реклама» и токен в подписи."""

    SETTINGS = {"channels": ["download-16x9"], "auto_publish": True, "draft": True, "ad": True}

    def test_ad_needs_erid(self):
        project = self.create(scenario="article", text=ARTICLE, settings=self.SETTINGS, run=True)
        done = self.settle(project["id"])
        step = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(step["status"], "failed", done["log"])
        self.assertIn("erid не указан", step["error"])
        self.assertFalse((self.folder(project["id"]) / "publish").exists())

        url = f"/api/studio/projects/{project['id']}"
        status, _ = self.json("POST", f"{url}/settings", {"erid": "2VtzqxABC12"})
        self.assertEqual(status, 200)
        status, view = self.json("POST", f"{url}/publish", {"channels": ["download-16x9"]})
        self.assertEqual(status, 202, view)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        description = (self.folder(project["id"]) / "publish" / "download-16x9" / "description.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("Реклама. erid: 2VtzqxABC12", description)

    def test_ad_line_survives_caption_cut(self):
        caption = publish.caption(publish.with_ad(long_post(3000), "abc123"), "T")
        self.assertTrue(caption.rstrip().endswith("Реклама. erid: abc123"))

    def test_bad_erid(self):
        payload = {"scenario": "post", "post": "Пост", "settings": {"erid": "две части"}}
        self.assertEqual(self.json("POST", "/api/studio/projects", payload)[0], 400)


if __name__ == "__main__":
    unittest.main()
