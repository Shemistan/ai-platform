"""core/media: есть ли в сборке ffmpeg нужный фильтр (QA-028)."""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from aiplatform.core import media


def fake_ffmpeg(folder: Path, filters: str) -> str:
    """Скрипт вместо ffmpeg: на `-filters` печатает заданный список."""
    path = folder / "ffmpeg"
    path.write_text(f"#!/bin/sh\ncat <<'EOF'\nFilters:\n{filters}\nEOF\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


class HasFilterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        media.has_filter.cache_clear()
        self.addCleanup(media.has_filter.cache_clear)

    def test_full_build_has_drawtext(self):
        (self.dir / "full").mkdir()
        cmd = fake_ffmpeg(self.dir / "full", " T.C drawtext          V->V       Draw text.\n ..C scale  V->V  Scale.")
        self.assertTrue(media.has_filter(cmd, "drawtext"))

    def test_slim_build_without_drawtext(self):
        # Формула ffmpeg в Homebrew без freetype: drawtext нет, «drawtext» в описании — не фильтр.
        (self.dir / "slim").mkdir()
        cmd = fake_ffmpeg(self.dir / "slim", " ..C scale  V->V  Scale, no drawtext here.")
        self.assertFalse(media.has_filter(cmd, "drawtext"))

    def test_missing_binary(self):
        self.assertFalse(media.has_filter(os.fspath(self.dir / "нет-ffmpeg"), "drawtext"))


if __name__ == "__main__":
    unittest.main()
