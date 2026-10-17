"""Баннер «Проблемы»: без claude CLI в PATH /api/config говорит об этом (DX-013)."""

import dataclasses
import os
import shutil
import tempfile
import unittest
from unittest import mock

from aiplatform.news.service import NewsModule
from aiplatform.studio.llm import missing_cli
from tests.helpers import PlatformCase

_which = shutil.which


def without_claude(cmd, *args, **kwargs):
    """shutil.which, будто claude в PATH нет; остальное — как есть."""
    return None if os.path.basename(cmd) == "claude" else _which(cmd, *args, **kwargs)


class MissingCliTextTest(unittest.TestCase):
    def test_claude_gets_install_hint(self):
        for cmd in ("claude", "/opt/homebrew/bin/claude"):
            with self.subTest(cmd=cmd):
                text = missing_cli(cmd)
                self.assertIn("Нет claude CLI", text)
                self.assertIn("дайджест", text)
                self.assertIn("claude --version", text)

    def test_other_command_points_to_env(self):
        text = missing_cli("my-llm")
        self.assertIn("«my-llm»", text)
        self.assertIn("AIP_LLM_CMD", text)


class BannerHelpLinkTest(unittest.TestCase):
    def test_nav_links_to_existing_troubleshooting_section(self):
        """Баннер ведёт на «Если не работает при установке», и такой раздел в README есть."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "aiplatform", "static", "nav.js"), encoding="utf-8") as f:
            nav = f.read()
        with open(os.path.join(root, "README.md"), encoding="utf-8") as f:
            readme = f.read()
        self.assertIn("README.md#если-не-работает-при-установке", nav)
        self.assertIn("Что делать", nav)
        self.assertIn("### Если не работает при установке", readme)


class NewsWithoutClaudeTest(PlatformCase):
    """Настоящий бэкенд, вкладка «Новости», claude нет — проблема в /api/config одна."""

    @staticmethod
    def modules(ctx):
        return [NewsModule(ctx)]

    def setUp(self):
        self.narrator_dir = tempfile.TemporaryDirectory()  # пустой каталог: движка нет, проверка быстрая
        self.addCleanup(self.narrator_dir.cleanup)
        self.extra_env = {"AIP_BACKEND": "real", "AIP_NARRATOR_DIR": self.narrator_dir.name}
        patcher = mock.patch("shutil.which", side_effect=without_claude)
        patcher.start()
        self.addCleanup(patcher.stop)
        super().setUp()

    def test_config_reports_missing_claude(self):
        status, body = self.json("GET", "/api/config")
        self.assertEqual(status, 200)
        found = [p for p in body["problems"] if "Нет claude CLI" in p]
        self.assertEqual(len(found), 1, body["problems"])

    def test_no_problem_when_claude_present(self):
        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            _, body = self.json("GET", "/api/config")
        self.assertFalse([p for p in body["problems"] if "claude CLI" in p], body["problems"])


class StudioWithoutClaudeTest(PlatformCase):
    """Студия с командой claude по умолчанию: тот же текст, что у новостей (баннер склеит)."""

    def test_studio_problem_matches_news(self):
        studio = self.app.modules["studio"]
        real = dataclasses.replace(studio.config, fake=False, llm_cmd=("claude", "-p"))
        with mock.patch("shutil.which", side_effect=without_claude), mock.patch.object(studio, "config", real):
            problems = studio.problems()
        self.assertIn(missing_cli("claude"), problems)


if __name__ == "__main__":
    unittest.main()
