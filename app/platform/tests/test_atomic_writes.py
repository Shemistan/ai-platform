"""Атомарная запись (BE-010): общий хелпер, кэш генераций, замена постера."""

import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.core import fsutil
from aiplatform.studio import service as service_module
from aiplatform.studio import steps
from aiplatform.studio.cache import MediaCache
from tests.test_studio_flow import ARTICLE, StudioCase, png_bytes


class AtomicWriteTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_write_and_failure_keeps_old(self):
        path = self.dir / "sub" / "a.json"
        fsutil.atomic_write(path, "старое")
        with mock.patch.object(fsutil.os, "replace", side_effect=OSError("диск")), self.assertRaises(OSError):
            fsutil.atomic_write(path, "новое")
        self.assertEqual(path.read_text(encoding="utf-8"), "старое")
        self.assertEqual(sorted(p.name for p in path.parent.iterdir()), ["a.json"], "tmp убран")
        fsutil.atomic_write(path, b"\x00\x01", mode=0o600)
        self.assertEqual((path.read_bytes(), path.stat().st_mode & 0o777), (b"\x00\x01", 0o600))

    def test_tmp_names_unique(self):
        path = self.dir / "x.txt"
        self.assertNotEqual(fsutil.tmp_path(path), fsutil.tmp_path(path))

    def test_parallel_cache_put_same_entry(self):
        cache = MediaCache(self.dir / "cache")
        sources = []
        for n in range(2):
            src = self.dir / f"src{n}.png"
            src.write_bytes(bytes([n]) * 200_000)
            sources.append(src)
        errors = []

        def put(src):
            try:
                for _ in range(30):
                    cache.put("entry", src)
            except Exception as exc:  # noqa: BLE001 — любой сбой потока — провал теста
                errors.append(exc)

        threads = [threading.Thread(target=put, args=(s,)) for s in sources]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(errors, [])
        found = cache.find("entry")
        self.assertIsNotNone(found)
        self.assertIn(found.read_bytes(), [s.read_bytes() for s in sources], "файл целый — один из двух")
        self.assertEqual([p.name for p in (self.dir / "cache").iterdir() if p.name.endswith(".tmp")], [])

    def test_slide_media_replaced_new_first(self):
        folder = self.dir / "images"
        steps.save_slide_media(folder, 1, "png", b"png", "промпт", steps.IMAGES)
        steps.save_slide_media(folder, 1, "jpg", b"jpg", "промпт", steps.IMAGES)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["01.jpg", "01.json"])


class PosterReplaceTest(StudioCase):
    def test_failed_write_keeps_old_poster(self):
        project = self.create(scenario="article", text=ARTICLE)
        pid = project["id"]
        first = png_bytes()
        self.assertEqual(self.call("POST", f"/api/studio/projects/{pid}/upload/poster", first)[0], 200)
        poster = steps.find(self.folder(pid), "poster", steps.IMAGE_EXT)
        self.assertIsNotNone(poster)
        with mock.patch.object(service_module, "atomic_write", side_effect=OSError("диск полон")):
            status, _, _ = self.call("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())
        self.assertEqual(status, 500)
        self.assertEqual(poster.read_bytes(), first, "прежний постер на месте")


if __name__ == "__main__":
    unittest.main()
