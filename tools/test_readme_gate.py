#!/usr/bin/env python3
"""Тесты tools/readme_gate.py: python3 tools/test_readme_gate.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import readme_gate  # noqa: E402


class ReadmeGateTest(unittest.TestCase):
    def _file(self, n: int) -> str:
        fd, path = tempfile.mkstemp(suffix=".md")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("строка\n" * n)
        self.addCleanup(os.remove, path)
        return path

    def test_within_limit(self):
        self.assertIsNone(readme_gate.check(self._file(200)))

    def test_over_limit_says_where(self):
        err = readme_gate.check(self._file(201))
        self.assertIn("201 строк", err)
        self.assertIn("docs/user-guide.md", err)
        self.assertIn("docs/configuration.md", err)

    def test_missing_file_ok(self):
        self.assertIsNone(readme_gate.check("/нет/такого/README.md"))

    def test_repo_readme_fits(self):
        self.assertEqual(readme_gate.main(), 0)


if __name__ == "__main__":
    unittest.main()
