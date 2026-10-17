"""Вкладка «Обновления» и пометки о запрещённых организациях в выпусках."""

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from aiplatform.studio import slides, steps
from aiplatform.studio.render import Layout, slide_filter
from aiplatform.updates import checks, registry
from tests.helpers import PlatformCase
from tests.test_studio_flow import NEEDS_FFMPEG, StudioCase

DEFAULT = registry.Registry(registry.DEFAULT_FILE).entries()
META_NOTE = (
    "Компания Meta (продукты Facebook и Instagram) признана экстремистской организацией и запрещена на территории РФ."
)


def ids(text):
    return [e.id for e in registry.mentioned(text, DEFAULT)]


class RegistryTest(unittest.TestCase):
    def test_default_list(self):
        self.assertEqual(
            [e.id for e in DEFAULT],
            ["meta", "cyberpartisans", "silent-crow", "almaz-capital", "recorded-future", "opentext"],
        )
        self.assertEqual(registry.notes("Meta", DEFAULT), [META_NOTE])

    def test_matching(self):
        found = {
            "Компания Meta выпустила очки": ["meta"],
            "в Инстаграме и Фейсбуке": ["meta"],
            "Facebook-аккаунт": ["meta"],
            "INSTAGRAM": ["meta"],
            "мета-анализ показал": [],
            "Мета-анализ показал": [],
            "метаданные и metadata": [],
            "WhatsApp обновился": [],  # решение на WhatsApp не распространяется
            "хакеры из «Киберпартизан»": ["cyberpartisans"],
            "Silent Crow взломали": ["silent-crow"],
            "Александра Галицкого": ["almaz-capital"],
            "Recorded Future и OpenText": ["recorded-future", "opentext"],
            "Go 1.26 вышел": [],
        }
        for text, want in found.items():
            with self.subTest(text=text):
                self.assertEqual(ids(text), want)

    def test_footnotes_once(self):
        post = registry.with_footnotes("**Главное** за день.", "Instagram запустил ленту", DEFAULT)
        self.assertEqual(post, f"**Главное** за день.\n\n* {META_NOTE}\n")
        self.assertEqual(registry.with_footnotes(post, "Instagram", DEFAULT), post)
        self.assertEqual(registry.with_footnotes("Про Go", "про Go", DEFAULT), "Про Go")
        self.assertEqual(registry.with_footnotes("", "Meta", DEFAULT), f"* {META_NOTE}\n")

    def test_parse_errors(self):
        good = {"id": "x", "name": "X", "status": "extremist", "aliases": ["X Corp"], "disclaimer": "X запрещена"}
        bad = [
            ({"entries": "x"}, "entries"),
            ([good | {"id": "Плохо"}], "id"),
            ([good, good], "повторяется"),
            ([good | {"status": "bad"}], "status"),
            ([good | {"aliases": []}], "псевдонимов"),
            ([good | {"aliases": ["*"]}], "псевдонимов"),
            ([good | {"disclaimer": " "}], "disclaimer"),
        ]
        for obj, message in bad:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                registry.parse(obj)
        self.assertEqual(registry.parse([good])[0].disclaimer, "X запрещена")

    def test_diff_and_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = registry.Registry(Path(tmp) / "registry.json")
            self.assertFalse(store.customized())
            self.assertEqual(store.entries(), DEFAULT)
            store.save(DEFAULT[:2], "2026-10-01")
            self.assertEqual(store.read(), (DEFAULT[:2], "2026-10-01"))
            self.assertEqual(
                registry.diff(DEFAULT[:3], [DEFAULT[1], dataclasses.replace(DEFAULT[2], name="Y")]),
                {"added": [], "removed": ["meta"], "changed": ["silent-crow"]},
            )
            store.path.write_text("{битый", encoding="utf-8")
            self.assertEqual(store.entries(), DEFAULT, "испорченный файл — откат на список по умолчанию")
            store.reset()
            self.assertFalse(store.customized())


class SlideNotesTest(unittest.TestCase):
    def test_notes_follow_paragraphs_of_slide(self):
        paras = ["Вступление про Go.", "Meta выпустила модель.", "Ещё про Meta.", "Итог без компаний."]
        plan = [
            slides.Slide(para=0, title="Go"),
            slides.Slide(para=1, title="Новости"),
            slides.Slide(para=3, title="Итог", bullets=["и снова Instagram"]),
        ]
        self.assertEqual(steps.slide_notes(paras, plan, DEFAULT), [[], [META_NOTE], [META_NOTE]])

    def test_filter_draws_note_at_bottom(self):
        lay = Layout(480, 270, 17, 11, 24, 162, 97, 44, 70, 8, 130)
        with tempfile.TemporaryDirectory() as tmp:
            title, note = Path(tmp) / "t.txt", Path(tmp) / "n.txt"
            title.write_text("Заголовок", encoding="utf-8")
            note.write_text("строка 1\nстрока 2", encoding="utf-8")
            without = slide_filter(lay, "", title, [], 3.0)
            with_note = slide_filter(lay, "", title, [], 3.0, note)
        self.assertNotIn("n.txt", without)
        self.assertIn("n.txt", with_note)
        self.assertTrue(with_note.endswith(":y=240"), "две строки по 10 px над отступом снизу")


class ChecksTest(unittest.TestCase):
    def comp(self, kind, installed="", **kw):
        return checks.Component("x", "Фото", "x", kind, kw.pop("source", "o/r"), installed, **kw)

    def test_versions(self):
        fetch = {
            "https://pypi.org/pypi/o/r/json": {"info": {"version": "0.10.0"}},
            "https://api.github.com/repos/o/r/releases/latest": {"tag_name": "v3.1.0"},
            "https://api.github.com/repos/o/r/tags?per_page=50": [{"name": "v0.9.9"}, {"name": "v0.16.2"}],
        }.__getitem__
        self.assertEqual(checks.check(self.comp("pypi", "0.9.2"), fetch).status, checks.UPDATE)
        self.assertEqual(checks.check(self.comp("release", "3.1.0"), fetch).status, checks.OK)
        self.assertEqual(checks.check(self.comp("tag", "0.16.2"), fetch).latest, "0.16.2")
        self.assertEqual(checks.check(self.comp("pypi"), fetch).status, checks.UNKNOWN)

    def test_errors_do_not_raise(self):
        def offline(url):
            raise OSError("нет сети")

        result = checks.check(self.comp("pypi", "1.0"), offline)
        self.assertEqual((result.status, result.message), (checks.ERROR, "Не удалось проверить: нет сети"))

    def test_hf_weights_and_pinned_revision(self):
        newer = {"sha": "abcdef1234", "lastModified": "2026-09-20T00:00:00.000Z"}
        installed_at = 1780000000.0  # 2026-05-28
        weights = self.comp("hf", "установлены", installed_at=installed_at)
        self.assertEqual(checks.check(weights, lambda url: newer).status, checks.UPDATE)
        same_day = {"sha": "x", "lastModified": "2026-05-28T12:00:00.000Z"}
        self.assertEqual(checks.check(weights, lambda url: same_day).status, checks.OK)
        self.assertEqual(checks.check(self.comp("hf", "abcdef1"), lambda url: newer).status, checks.OK)
        self.assertEqual(checks.check(self.comp("hf", "1234567"), lambda url: newer).status, checks.UPDATE)

    def test_versions_from_install_paths(self):
        self.assertEqual(checks._version_in("/x/flux2cli-v3.1.0/Flux2CLI", "flux2cli-"), "3.1.0")
        self.assertEqual(checks._mflux_version(Path("/x/mflux-0.22.0/bin")), "0.22.0")
        self.assertEqual(checks.version_key("v0.16.10") > checks.version_key("0.16.9"), True)


class UpdatesTabTest(PlatformCase):
    def done(self, body, key):
        part = body["models"] if key == "models" else body["registry"]["check"]
        return part.get("status") in ("done", "failed")

    def test_models_check(self):
        status, body = self.json("POST", "/api/updates/models/check")
        self.assertEqual(status, 200, body)
        body = self.poll("/api/updates/state", lambda b: self.done(b, "models"))
        self.assertEqual(body["models"]["status"], "done", body["models"])
        by_id = {r["id"]: r for r in body["models"]["results"]}
        self.assertEqual(by_id["mflux"]["status"], "update")
        self.assertEqual((by_id["mflux"]["installed"], by_id["mflux"]["latest"]), ("0.22.0", "0.23.1"))
        self.assertIn("MFLUX_VERSION", by_id["mflux"]["hint"])
        self.assertEqual(by_id["flux2cli"]["status"], "ok")
        self.assertEqual(by_id["ltx-2-mlx"]["latest"], "0.17.0")
        self.assertEqual(by_id["photo-z-image-turbo"]["status"], "update")
        self.assertEqual(by_id["photo-krea-2"]["status"], "ok")
        self.assertEqual(by_id["video-wan-2.2-t2v-q4"]["status"], "ok")
        self.assertNotIn("narrator", by_id)  # движок — часть репозитория, сверять не с чем
        self.assertEqual(body["models"]["updates"], 3)
        status, _, page = self.call("GET", "/updates/")
        self.assertEqual(status, 200)
        self.assertIn(b"app.js", page)

    def test_registry_check_apply_edit_reset(self):
        _, body = self.json("GET", "/api/updates/state")
        self.assertEqual(len(body["registry"]["entries"]), 6)
        self.assertFalse(body["registry"]["customized"])

        self.json("POST", "/api/updates/registry/check")
        body = self.poll("/api/updates/state", lambda b: self.done(b, "registry"))
        check = body["registry"]["check"]
        self.assertEqual(check["status"], "done", check)
        self.assertEqual(check["diff"], {"added": ["demo-corp"], "removed": [], "changed": []})
        self.assertIn("Демо", check["notes"])
        status, body = self.json("POST", "/api/updates/registry/apply")
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["registry"]["entries"]), 7)
        self.assertTrue(body["registry"]["customized"])
        self.assertIsNone(body["registry"]["check"]["proposal"])
        self.assertEqual(self.json("POST", "/api/updates/registry/apply")[0], 400)

        # Повторная сверка: изменений нет — предложения нет, дата сверки обновлена.
        self.json("POST", "/api/updates/registry/check")
        body = self.poll("/api/updates/state", lambda b: self.done(b, "registry"))
        self.assertIsNone(body["registry"]["check"]["proposal"])

        entries = [e for e in body["registry"]["entries"] if e["id"] != "demo-corp"]
        status, body = self.json("POST", "/api/updates/registry", {"text": json.dumps({"entries": entries[:1]})})
        self.assertEqual(status, 200, body)
        self.assertEqual([e["id"] for e in body["registry"]["entries"]], ["meta"])
        for bad in ("{", json.dumps({"entries": []}), json.dumps({"entries": [{"id": "x"}]})):
            self.assertEqual(self.json("POST", "/api/updates/registry", {"text": bad})[0], 400, bad)
        _, body = self.json("POST", "/api/updates/registry/reset")
        self.assertEqual(len(body["registry"]["entries"]), 6)


ARTICLE_META = """Meta открыла модель Llama

Компания Meta выложила новую версию модели. Её уже можно скачать.

Go 1.26 ускорил сборку мусора. Паузы стали короче.

Итог: обновляйтесь. Подписывайтесь на канал."""


@NEEDS_FFMPEG
class DisclaimerFlowTest(StudioCase):
    def test_video_and_post_get_disclaimer(self):
        settings = {"channels": ["download-16x9"], "auto_publish": True}
        project = self.create(scenario="article", text=ARTICLE_META, settings=settings, run=True)
        self.assertEqual(project["disclaimers"], [{"name": "Meta", "note": META_NOTE}])
        self.assertTrue(project["settings"]["disclaimers"])
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertIn("пометки о запрещённых организациях: слайды 1, 2", "\n".join(done["log"]))
        folder = self.folder(project["id"])
        clips = folder / ".video-clips"
        self.assertIn("Meta", (clips / "01" / "note.txt").read_text(encoding="utf-8"))
        self.assertFalse((clips / "03" / "note.txt").exists(), "у слайда без Meta пометки нет")
        description = (folder / "publish" / "download-16x9" / "description.txt").read_text(encoding="utf-8")
        self.assertIn(META_NOTE, description)

    def test_disclaimers_can_be_off(self):
        settings = {"disclaimers": False, "channels": ["download-16x9"], "auto_publish": True}
        project = self.create(scenario="article", text=ARTICLE_META, settings=settings, run=True)
        done = self.settle(project["id"])
        # Упоминание есть, пометки выключены — без причины не публикуется (CMP-004).
        self.assertEqual(self.statuses(done)["publish"], "failed", done["log"])
        body = {"channels": ["download-16x9"], "ignore_disclaimers": "проверка"}
        self.json("POST", f"/api/studio/projects/{project['id']}/publish", body)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertNotIn("пометки о запрещённых организациях", "\n".join(done["log"]))
        description = (self.folder(project["id"]) / "publish" / "download-16x9" / "description.txt").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("экстремистской", description)


class VoiceModelsTest(PlatformCase):
    process_env = {"FAKE_NARRATOR_DELAY": "0.01"}

    def task(self, task):
        return self.poll(f"/api/updates/tasks/{task['id']}", lambda t: t["status"] not in ("queued", "running"))

    def test_listing_and_downloads(self):
        status, data = self.json("GET", "/api/updates/voice-models")
        self.assertEqual(status, 200, data)
        main = {m["id"]: m["exists"] for m in data["main"]}
        self.assertFalse(main["silero"])
        mlx = {m["id"]: m["downloaded"] for m in data["mlx"]}
        self.assertTrue(mlx["higgs"])
        self.assertFalse(mlx["fish"])

        _, task = self.json("POST", "/api/updates/voice-models/fetch", {})
        self.assertEqual(self.task(task)["status"], "done")
        _, data = self.json("GET", "/api/updates/voice-models")
        self.assertTrue(all(m["exists"] for m in data["main"]))

        self.assertEqual(self.json("POST", "/api/updates/voice-models/mlx", {"engines": ["nope"]})[0], 400)
        _, task = self.json("POST", "/api/updates/voice-models/mlx", {"engines": ["fish", "qwen3"]})
        self.assertIn("fish, qwen3", task["title"])
        self.assertEqual(self.task(task)["status"], "done")
        _, data = self.json("GET", "/api/updates/voice-models")
        self.assertTrue({m["id"]: m["downloaded"] for m in data["mlx"]}["qwen3"])

        _, task = self.json("POST", "/api/updates/voice-models/move", {})
        self.assertEqual(self.task(task)["status"], "done")
        self.assertEqual(self.json("POST", "/api/updates/voice-models/nope", {})[0], 404)


if __name__ == "__main__":
    unittest.main()
