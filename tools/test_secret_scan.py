#!/usr/bin/env python3
"""Тесты tools/secret_scan.py: python3 tools/test_secret_scan.py"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import secret_scan  # noqa: E402

# Похоже на настоящий токен, но собрано в рантайме — сам файл скан не ловит.
REALISH = "84" + "41726324" + ":" + "AAGx9" + "Qz7pLk2Mn4Bv8Cx1Ws5Ed3Rf6Tg0Yh"


class FindTokensTest(unittest.TestCase):
    def test_real_looking_token_found(self):
        self.assertEqual(len(REALISH.split(":")[1]), 35)
        hits = secret_scan.find_tokens(f"x\nTOKEN = '{REALISH}'\n")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0][0], 2)
        self.assertNotIn(REALISH, hits[0][1])

    def test_synthetic_skipped(self):
        for fake in ("123456789:" + "A" * 35, "11111111:" + "Ab1_" * 8 + "Ab1"):
            self.assertEqual(secret_scan.find_tokens(fake), [], fake)

    def test_short_parts_ignored(self):
        self.assertEqual(secret_scan.find_tokens("123456:" + "A" * 35), [])
        self.assertEqual(secret_scan.find_tokens("111:AAAA"), [])


class ScanRepoTest(unittest.TestCase):
    def setUp(self):
        # Под pre-commit git задаёт GIT_INDEX_FILE / GIT_DIR настоящего репозитория:
        # git во временном репо (и в secret_scan.scan) пошёл бы в чужой индекс.
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            self.addCleanup(os.environ.__setitem__, key, os.environ.pop(key))
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.git("init", "-q")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        subprocess.run(["git", "-C", self.root, *args], check=True,
                       capture_output=True)

    def put(self, name, text):
        with open(os.path.join(self.root, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_clean_tree_passes(self):
        self.put("a.py", "FAKE = '123456:' + 'A' * 35\n")
        self.git("add", "a.py")
        self.assertEqual(secret_scan.scan(self.root), [])

    def test_tracked_token_fails(self):
        self.put("a.py", f"T = '{REALISH}'\n")
        self.git("add", "a.py")
        hits = secret_scan.scan(self.root)
        self.assertEqual([(p, n) for p, n, _ in hits], [("a.py", 1)])
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(secret_scan.main(["x", self.root]), 1)
        self.assertIn("a.py:1", err.getvalue())
        self.assertNotIn(REALISH, err.getvalue())

    def test_untracked_ignored(self):
        self.put("bot_token.md", REALISH)
        self.assertEqual(secret_scan.scan(self.root), [])


if __name__ == "__main__":
    unittest.main()
