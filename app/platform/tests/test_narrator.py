"""Вызов движка narrator: мост `narrate bridge` и команды на фейковом движке."""

import tempfile
import unittest
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.core import narrator


class NarratorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = platform_config.load({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp.name})
        self.narrator = narrator.load(config)

    def test_real_defaults_point_at_app_narrator(self):
        config = platform_config.load({"AIP_DATA_DIR": "/tmp/x"})
        real = narrator.load(config)
        self.assertEqual(real.home.name, "narrator")
        self.assertEqual(real.home.parent.name, "app")
        self.assertEqual(real.cmd, (str(real.home / "narrate"),))
        self.assertEqual(
            real.make("news-rescue", DAY="2026-10-01", X=""),
            ["make", "-C", str(real.home), "news-rescue", "DAY=2026-10-01"],
        )

    def test_bridge_engines_and_preview(self):
        engines = self.narrator.bridge("engines")
        by_id = {e["id"]: e for e in engines["engines"]}
        self.assertIn("модель не скачана", by_id["fish"]["problem"])
        self.assertIsNone(by_id["higgs"]["problem"])
        preview = self.narrator.bridge("preview", stdin="Go 26\n")
        self.assertEqual(preview["script"], "го два шесть\n")
        self.assertEqual(preview["changed"], 1)

    def test_bridge_error_and_state(self):
        with self.assertRaisesRegex(narrator.NarratorError, "Нужны название и текст"):
            self.narrator.bridge("article-new", stdin={"title": "x"})
        made = self.narrator.bridge("article-new", stdin={"title": "Проба", "text": "Текст."})
        library = self.narrator.bridge("library")
        self.assertIn(made["id"], [a["id"] for a in library["articles"]])
        self.assertTrue(Path(self.narrator.env[narrator.FAKE_HOME_ENV], "state.json").is_file())
        with self.assertRaisesRegex(narrator.NarratorError, "нет в библиотеке"):
            self.narrator.bridge("article", "nope")

    def test_missing_command(self):
        broken = narrator.Narrator(("/нет/такой/narrate",), Path("/нет"))
        self.assertFalse(broken.available())
        with self.assertRaisesRegex(narrator.NarratorError, "не найдена"):
            broken.bridge("engines")


if __name__ == "__main__":
    unittest.main()
