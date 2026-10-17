#!/usr/bin/env python3
"""Тесты tools/advance-staging.sh: python3 tools/test_advance_staging.py"""
import os
import subprocess
import tempfile
import unittest

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "advance-staging.sh")


class AdvanceStagingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # Из pre-commit приходят GIT_INDEX_FILE, GIT_DIR…: временный репозиторий их не должен видеть.
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        self.env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        self.git("init", "-q", "-b", "main")
        self.commit("base")
        self.git("branch", "staging")

    def git(self, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", *args], cwd=self.tmp.name,
                              env=self.env, check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, msg):
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def run_script(self, *args):
        return subprocess.run(["sh", SCRIPT, *args], cwd=self.tmp.name, env=self.env,
                              capture_output=True, text=True)

    def test_fast_forward(self):
        self.git("switch", "-q", "-c", "task")
        head = self.commit("work")
        res = self.run_script()
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.git("rev-parse", "staging"), head)

    def test_refuses_when_staging_moved_ahead(self):
        # Другой агент уже сдвинул staging: наш HEAD её не содержит.
        self.git("switch", "-q", "-c", "other")
        foreign = self.commit("SEC")
        self.git("update-ref", "refs/heads/staging", foreign)
        self.git("switch", "-q", "main")
        self.git("switch", "-q", "-c", "task")
        self.commit("work")
        res = self.run_script()
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("не предок", res.stderr)
        self.assertEqual(self.git("rev-parse", "staging"), foreign)

    def test_noop_when_already_there(self):
        res = self.run_script("staging")
        self.assertEqual(res.returncode, 0, res.stderr)


if __name__ == "__main__":
    unittest.main()
