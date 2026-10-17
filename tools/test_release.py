#!/usr/bin/env python3
"""Тесты tools/release.py: python3 tools/test_release.py"""
import datetime as dt
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import release  # noqa: E402

DAY = dt.date(2026, 10, 17)
FILES = {
    "app/platform/pyproject.toml": '[project]\nname = "aiplatform"\nversion = "0.1.0"\n',
    "app/narrator/pyproject.toml": '[project]\nname = "narrator"\nversion = "0.1.0"\n',
    "app/platform/aiplatform/__init__.py": '__version__ = "0.1.0"  # как в pyproject.toml\n',
    "app/narrator/uv.lock": '[[package]]\nname = "numpy"\nversion = "2.0.0"\n\n'
    '[[package]]\nname = "narrator"\nversion = "0.1.0"\nsource = { editable = "." }\n',
    "docs/CHANGELOG.md": "# Журнал версий\n\n<!-- release:next -->\n\n## 0.1.0 — до версий\n",
}


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        # Из pre-commit приходят GIT_INDEX_FILE, GIT_DIR…: временный репозиторий их не должен видеть.
        clean = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        patcher = mock.patch.dict(os.environ, clean, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        for rel, text in FILES.items():
            path = os.path.join(self.root, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.com")
        self.git("config", "user.name", "t")
        self.git("config", "commit.gpgsign", "false")
        self.git("config", "core.hooksPath", "/dev/null")
        self.commit("chore: начало")
        self.backlog = os.path.join(self.root, "backlog")
        os.makedirs(os.path.join(self.backlog, "tasks"))
        with open(os.path.join(self.backlog, "tasks", "BE-001-x.md"), "w", encoding="utf-8") as f:
            f.write("---\nid: BE-001\n---\n\n# Публикация без дублей\n")

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True, text=True).stdout

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)

    def read(self, rel):
        with open(os.path.join(self.root, rel), encoding="utf-8") as f:
            return f.read()

    def test_next_version(self):
        self.assertEqual(release.next_version(DAY, []), "2026.10.17")
        self.assertEqual(release.next_version(DAY, ["v2026.10.17"]), "2026.10.17.2")
        self.assertEqual(release.next_version(DAY, ["v2026.10.17", "v2026.10.17.2"]), "2026.10.17.3")
        self.assertEqual(release.next_version(DAY, ["v2026.10.16"]), "2026.10.17")

    def test_prep_bumps_everything_and_writes_changelog(self):
        self.commit("feat(studio): публикация (BE-001)")
        self.commit("fix(ui): без задачи")
        self.commit("docs: две задачи (DX-003, DX-006)")
        version = release.prep(self.root, DAY, self.backlog)
        self.assertEqual(version, "2026.10.17")
        self.assertEqual(set(release.versions(self.root).values()), {"2026.10.17"})
        self.assertIn('name = "numpy"\nversion = "2.0.0"', self.read("app/narrator/uv.lock"))
        log = self.read("docs/CHANGELOG.md")
        self.assertIn("<!-- release:next -->\n\n## 2026.10.17 — 2026-10-17\n", log)
        self.assertIn("- BE-001 — Публикация без дублей", log)  # заголовок из бэклога
        self.assertIn("- DX-003 — docs: две задачи (DX-003, DX-006)", log)  # нет в бэклоге — сабж
        self.assertLess(log.index("2026.10.17"), log.index("0.1.0"))

    def test_tag_matches_version_and_blocks_repeat_prep(self):
        release.prep(self.root, DAY, None)
        with self.assertRaises(release.ReleaseError):  # подготовлено, но без тега
            release.prep(self.root, DAY, None)
        self.commit("chore(release): 2026.10.17")
        self.assertEqual(release.tag(self.root), "v2026.10.17")
        self.assertIn("v2026.10.17", self.git("tag", "--list"))
        with self.assertRaises(release.ReleaseError):  # тег уже есть
            release.tag(self.root)
        self.commit("feat: ещё (BE-001)")
        self.assertEqual(release.prep(self.root, DAY, None), "2026.10.17.2")
        log = self.read("docs/CHANGELOG.md")
        self.assertIn("## 2026.10.17.2 — 2026-10-17\n\n- BE-001 — feat: ещё (BE-001)", log)
        self.assertNotIn("chore(release)", log.split("## 2026.10.17 —")[0])

    def test_tag_refuses_initial_and_mismatch(self):
        with self.assertRaises(release.ReleaseError):
            release.tag(self.root)
        release.set_version(self.root, "2026.10.17")
        path = os.path.join(self.root, "app/narrator/pyproject.toml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('[project]\nversion = "0.1.0"\n')
        with self.assertRaisesRegex(release.ReleaseError, "расходятся"):
            release.tag(self.root)

    def test_repo_versions_agree(self):
        """В самом репозитории версия везде одна — иначе /api/health врёт."""
        release.current(release.ROOT)


if __name__ == "__main__":
    unittest.main()
