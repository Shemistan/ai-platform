"""Повторное использование: дубликат выпуска, копия выпуска, кэш картинок, роликов и озвучки."""

import tempfile
import unittest
from pathlib import Path

from aiplatform.studio import cache
from aiplatform.studio.service import source_id
from tests.test_studio_flow import ARTICLE, NEEDS_FFMPEG, StudioCase


class CacheUnitTest(unittest.TestCase):
    def test_entry_by_inputs_and_take_put(self):
        self.assertEqual(cache.entry_id("image", prompt="a", model="m"), cache.entry_id("image", model="m", prompt="a"))
        self.assertNotEqual(cache.entry_id("image", prompt="a"), cache.entry_id("image", prompt="b"))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = cache.MediaCache(root / "cache")
            entry = cache.entry_id("voice", text="привет")
            self.assertFalse(store.take(entry, root / "out.mp3"))
            (root / "voice.mp3").write_bytes(b"ID3 old")
            store.put(entry, root / "voice.mp3")
            self.assertTrue(store.take(entry, root / "p" / "voice.mp3"))
            # Копия независима: правка файла выпуска не портит запись кэша.
            (root / "p" / "voice.mp3").write_bytes(b"changed")
            self.assertEqual(store.find(entry).read_bytes(), b"ID3 old")

    def test_source_ignores_whitespace(self):
        self.assertEqual(source_id("article", text="a  b\n\nc"), source_id("article", text="a b c"))
        self.assertEqual(source_id("digest", day="2026-10-15"), "digest:2026-10-15")


@NEEDS_FFMPEG
class ReuseTest(StudioCase):
    def jobs(self, tab):
        return len(self.json("GET", f"/api/{tab}/jobs")[1]["jobs"])

    def test_duplicate_article_and_digest(self):
        first = self.create(scenario="article", text=ARTICLE)
        status, body = self.json("POST", "/api/studio/projects", {"scenario": "article", "text": ARTICLE + "\n"})
        self.assertEqual(status, 409, body)
        self.assertEqual(body["existing"]["id"], first["id"])
        again = self.create(scenario="article", text=ARTICLE, force_new=True)
        self.assertNotEqual(again["id"], first["id"])

        digest = self.create(scenario="digest", day="2026-10-15")
        status, body = self.json("POST", "/api/studio/projects", {"scenario": "digest", "day": "2026-10-15"})
        self.assertEqual((status, body["existing"]["id"]), (409, digest["id"]))
        self.json("POST", f"/api/studio/projects/{digest['id']}/archive", {"archived": True})
        self.create(scenario="digest", day="2026-10-15")  # архивный не мешает

    def test_second_release_takes_media_from_cache(self):
        settings = {"shorts": True}
        first = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        self.settle(first["id"])
        photos, audios = self.jobs("photo"), self.jobs("audio")
        self.assertEqual((photos, audios), (5, 2))

        second = self.create(scenario="article", text=ARTICLE, settings=settings, run=True, force_new=True)
        done = self.settle(second["id"])
        self.assertEqual((self.jobs("photo"), self.jobs("audio")), (photos, audios), done["log"])
        self.assertTrue(any("из кэша" in line for line in done["log"]))
        self.assertEqual(done["files"]["images"].keys(), {"01", "03", "05", "07"})
        self.assertTrue((self.folder(second["id"]) / "video.mp4").is_file())

        # «Перезапустить шаг» просит новый вариант: кэш не читается.
        self.json("POST", f"/api/studio/projects/{second['id']}/run", {"step": "poster"})
        self.settle(second["id"])
        self.assertEqual(self.jobs("photo"), photos + 1)

    def test_clone_keeps_ready_results(self):
        source = self.create(scenario="article", text=ARTICLE, run=True)
        self.settle(source["id"])
        photos, audios = self.jobs("photo"), self.jobs("audio")
        status, copy = self.json("POST", f"/api/studio/projects/{source['id']}/clone")
        self.assertEqual(status, 201, copy)
        self.assertTrue(copy["title"].endswith("(копия)"))
        steps = self.statuses(copy)
        self.assertEqual([steps[s] for s in ("text", "poster", "plan", "images", "voice")], ["done"] * 5)
        self.assertEqual([steps[s] for s in ("render", "publish")], ["pending", "pending"])
        self.assertIsNone(copy["files"]["video"])
        self.assertTrue(copy["files"]["voice"])

        self.json("POST", f"/api/studio/projects/{copy['id']}/run", {})
        done = self.settle(copy["id"])
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertEqual((self.jobs("photo"), self.jobs("audio")), (photos, audios))


if __name__ == "__main__":
    unittest.main()
