"""Теги релизов и деплой живого checkout (REL-014): tools/release.sh в песочнице git."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from tests import TMP_ROOT

PLATFORM = Path(__file__).resolve().parents[1]
SCRIPT = PLATFORM / "tools" / "release.sh"
# Под pre-commit GIT_DIR / GIT_INDEX_FILE указывают на настоящий репозиторий — тесту они чужие.
CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


class ReleaseTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(dir=TMP_ROOT)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.git(self.repo, "init", "-q", "-b", "main")
        self.commit(self.repo, "a")
        self.calls = self.root / "calls.log"
        fake = self.root / "launchctl"
        fake.write_text(f'#!/bin/sh\necho "launchctl $@" >> "{self.calls}"\n')
        fake.chmod(0o755)
        self.env = {
            **CLEAN_ENV,
            "AIP_RELEASE_REMOTE": "",
            "AIP_RELEASE_DATE": "2026.10.17",
            "AIP_LOG_DIR": str(self.root / "logs"),
            "LAUNCHCTL": str(fake),
            "AIP_DEPLOY_BACKUP": f'echo backup >> "{self.calls}"',
        }

    def git(self, cwd, *args):
        return subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
            cwd=cwd,
            env=CLEAN_ENV,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    def commit(self, repo, name):
        (repo / name).write_text(name)
        self.git(repo, "add", name)
        self.git(repo, "commit", "-q", "--no-verify", "-m", name)

    def release(self, *args, cwd=None, **env):
        return subprocess.run(
            ["sh", str(SCRIPT), *args],
            cwd=cwd or self.repo,
            env={**self.env, **env},
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_tag_format_and_second_of_day(self):
        done = self.release("tag")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.git(self.repo, "tag"), "v2026.10.17")
        again = self.release("tag")
        self.assertIn("уже есть тег v2026.10.17", again.stdout)  # тот же коммит — без нового тега
        self.commit(self.repo, "b")
        self.assertEqual(self.release("tag").returncode, 0)
        self.assertEqual(self.git(self.repo, "tag").split(), ["v2026.10.17", "v2026.10.17.2"])

    def test_deploy_backs_up_checks_out_and_restarts(self):
        self.release("tag")
        self.commit(self.repo, "b")
        self.release("tag")
        live = self.root / "live"
        self.git(self.root, "clone", "-q", str(self.repo), str(live))
        env = {"AIP_LIVE_DIR": str(live)}

        done = self.release("deploy", "v2026.10.17", **env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.git(live, "describe", "--tags"), "v2026.10.17")
        calls = self.calls.read_text().splitlines()
        self.assertEqual(calls[0], "backup", "бэкап — до переключения")
        self.assertRegex(calls[1], r"^launchctl kickstart -k gui/\d+/com\.aiplatform\.server$")
        self.assertIn("откат: make deploy TAG=", done.stdout)

        # Откат — тот же deploy с прошлым тегом; журнал деплоев пишется.
        self.release("deploy", "v2026.10.17.2", **env)
        self.release("deploy", "v2026.10.17", **env)
        self.assertEqual(self.release("current", **env).stdout.strip(), "v2026.10.17")
        log = (self.root / "logs" / "deploy.log").read_text().splitlines()
        self.assertEqual(len(log), 3)
        self.assertTrue(log[-1].endswith("v2026.10.17.2 -> v2026.10.17"), log)

    def test_deploy_refuses_without_tag_or_on_failed_backup(self):
        self.release("tag")
        live = self.root / "live"
        self.git(self.root, "clone", "-q", str(self.repo), str(live))
        env = {"AIP_LIVE_DIR": str(live)}
        self.assertNotEqual(self.release("deploy", **env).returncode, 0)
        self.assertNotEqual(self.release("deploy", "v1999.01.01", **env).returncode, 0)
        failed = self.release("deploy", "v2026.10.17", AIP_DEPLOY_BACKUP="exit 1", **env)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("бэкап не удался", failed.stderr)
        self.assertFalse(self.calls.exists() and "kickstart" in self.calls.read_text())
        missing = self.release("deploy", "v2026.10.17", AIP_LIVE_DIR=str(self.root / "нет"))
        self.assertIn("git clone", missing.stderr)


if __name__ == "__main__":
    unittest.main()
