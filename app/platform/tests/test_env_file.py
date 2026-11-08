"""Секреты из .env в корне репозитория: разбор и загрузка в окружение сервера."""

import tempfile
import unittest
from pathlib import Path

from aiplatform import config as config_module
from tests.fixtures import TOKEN


class EnvFileTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.path = self.dir / "env"

    def test_parse(self):
        self.path.write_text(
            f"# секреты\n\nexport NARRATOR_TELEGRAM_TOKEN='{TOKEN}'\nB = \"два слова\"\nбез равно\n1BAD=x\nC=\n",
            encoding="utf-8",
        )
        self.assertEqual(
            config_module.read_env_file(self.path),
            {"NARRATOR_TELEGRAM_TOKEN": TOKEN, "B": "два слова", "C": ""},
        )
        self.assertEqual(config_module.read_env_file(self.dir / "нет"), {})

    def test_load_keeps_outer_values(self):
        self.path.write_text("NARRATOR_TELEGRAM_TOKEN=из-файла\nAIP_TG_NEWS=новости\n", encoding="utf-8")
        environ = {"AIP_ENV_FILE": str(self.path), "NARRATOR_TELEGRAM_TOKEN": "снаружи"}
        self.assertEqual(config_module.load_env_file(environ), self.path)
        self.assertEqual(environ["NARRATOR_TELEGRAM_TOKEN"], "снаружи")
        self.assertEqual(environ["AIP_TG_NEWS"], "новости")


if __name__ == "__main__":
    unittest.main()
