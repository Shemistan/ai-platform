"""Права на файлы с секретами и личными данными (SEC-010): запись 0600 и предупреждения."""

import tempfile
import unittest
from pathlib import Path

from aiplatform.channels.service import ChannelsModule
from aiplatform.core import narrator, perms
from aiplatform.studio import publish
from tests import TMP_ROOT
from tests.helpers import PlatformCase


def channels_only(ctx):
    return [ChannelsModule(ctx)]


class WritePrivateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(dir=TMP_ROOT))

    def test_new_and_rewritten_file_is_0600(self):
        path = self.tmp / "channels.json"
        path.write_text("[]", encoding="utf-8")
        path.chmod(0o644)
        publish.save_channels(path, [publish.Channel("tg-x", "X", publish.TELEGRAM, chat_id="@x")])
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["channels.json"])
        self.assertEqual(publish.load_channels(path, None)[0].chat_id, "@x")

    def test_warning_only_for_loose_mode(self):
        path = self.tmp / "secret.md"
        self.assertIsNone(perms.warning(path, "Файл"))  # нет файла — не проблема
        path.write_text("x", encoding="utf-8")
        path.chmod(0o600)
        self.assertIsNone(perms.warning(path, "Файл"))
        path.chmod(0o644)
        self.assertIn("chmod 600", perms.warning(path, "Файл"))
        folder = self.tmp / "data"
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        self.assertIn("chmod 700", perms.warning(folder, "Каталог"))


class ChannelsPermsTest(PlatformCase):
    modules = staticmethod(channels_only)

    def test_added_channel_file_is_0600(self):
        status, _ = self.json("POST", "/api/channels/channels", {"label": "Мой", "chat_id": "@my_chan"})
        self.assertEqual(status, 201)
        path = self.app.modules["channels"].config.channels_file
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_loose_token_file_is_a_problem(self):
        home = Path(self.tmp.name) / "narrator-home"
        home.mkdir()
        module = self.app.modules["channels"]
        module.narrator = narrator.Narrator(("/нет/такой/narrate",), home)
        token = home / "bot_token.md"
        token.write_text("не настоящий токен", encoding="utf-8")
        token.chmod(0o644)
        self.assertTrue(any("токена бота" in p and "chmod 600" in p for p in module.problems()), module.problems())
        token.chmod(0o600)
        self.assertFalse(any("токена бота" in p for p in module.problems()))

    def test_data_dir_created_private(self):
        self.assertEqual(self.config.data_dir.stat().st_mode & 0o077, 0)


if __name__ == "__main__":
    unittest.main()
