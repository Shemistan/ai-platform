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
        narrator.reset_checks()
        self.assertIn("не найдена", broken.check())


def _script(folder: Path, body: str) -> Path:
    path = folder / "narrate"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class CheckTest(unittest.TestCase):
    """check(): старый движок без bridge, без bridge version, чужая версия моста."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        narrator.reset_checks()
        self.addCleanup(narrator.reset_checks)

    def engine(self, body: str) -> narrator.Narrator:
        return narrator.Narrator((str(_script(self.dir, body)),), self.dir)

    def test_old_engine_without_bridge(self):
        old = self.engine(
            'echo "usage: narrate [-h] {say,synth} ..." >&2\n'
            "echo \"narrate: error: argument cmd: invalid choice: 'bridge' (choose from 'say', 'synth')\" >&2\n"
            "exit 2\n"
        )
        problem = old.check()
        self.assertIn("нет команды bridge", problem)
        self.assertIn(str(self.dir), problem)
        self.assertIn("AIP_NARRATOR_DIR", problem)

    def test_bridge_without_version(self):
        old = self.engine(
            "echo \"narrate bridge: error: invalid choice: 'version' (choose from 'engines')\" >&2\nexit 2\n"
        )
        self.assertIn("нет bridge version", old.check())

    def test_version_mismatch_and_ok(self):
        self.assertIn("версия моста 99, нужна", self.engine("echo '{\"bridge_api\": 99}'\n").check())
        narrator.reset_checks()
        ok = f"echo '{{\"bridge_api\": {narrator.EXPECTED_BRIDGE_API}}}'\n"
        self.assertIsNone(self.engine(ok).check())

    def test_result_is_cached(self):
        calls = self.dir / "calls"
        engine = self.engine(f'echo x >> "{calls}"\necho \'{{"bridge_api": {narrator.EXPECTED_BRIDGE_API}}}\'\n')
        self.assertIsNone(engine.check())
        self.assertIsNone(narrator.Narrator(engine.cmd, engine.home).check())  # другой экземпляр — тот же кеш
        self.assertEqual(calls.read_text().count("x"), 1)

    def test_fake_engine_is_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = platform_config.load({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp})
            self.assertIsNone(narrator.load(config).check())


if __name__ == "__main__":
    unittest.main()
