"""make smoke на фейках: прогон проходит и печатает таблицу шагов."""

import io
import os
import shutil
import unittest
from unittest import mock

from aiplatform import smoke
from tests.fixtures import FAST


@unittest.skipUnless(shutil.which("ffmpeg"), "нужен ffmpeg")
class SmokeTest(unittest.TestCase):
    def test_fake_run_reports_all_steps(self):
        out = io.StringIO()
        with mock.patch.dict(os.environ, FAST | {"AIP_RENDER_SCALE": "0.25"}):
            code = smoke.main(["--fake"], out=out)
        text = out.getvalue()
        self.assertEqual(code, 0, text)
        for step in ("Картинка", "Озвучка фразы", "Черновик статьи до видео"):
            self.assertIn(f"✓ {step}", text)
        self.assertIn("видео", text)


if __name__ == "__main__":
    unittest.main()
