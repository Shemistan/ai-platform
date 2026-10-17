"""Живость /api/health и установка LaunchAgent (tools/agent.sh) с подменённым launchctl."""

import os
import plistlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from aiplatform import __version__
from aiplatform.studio.schedule import Scheduler
from tests.helpers import PlatformCase

PLATFORM = Path(__file__).resolve().parents[1]


class HealthTest(PlatformCase):
    def test_health(self):
        status, body = self.json("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertIs(body["ok"], True)
        self.assertEqual(body["version"], __version__)
        self.assertIsInstance(body["uptime_s"], int)
        self.assertEqual(body["schedule"]["enabled"], False)
        self.assertEqual(body["schedule"]["last"], {})

    def test_config_has_version(self):
        """Версия видна и в /api/config: её показывает шапка (nav.js)."""
        status, body = self.json("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], __version__)


class HeartbeatTest(unittest.TestCase):
    def scheduler(self, env):
        module = SimpleNamespace(ctx=SimpleNamespace(config=SimpleNamespace(env=env)))
        return Scheduler(module, Path(tempfile.gettempdir()), tick_s=0)

    def test_off_by_default_and_pings_url(self):
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.scheduler({})._heartbeat()
            urlopen.assert_not_called()
            self.scheduler({"AIP_HEARTBEAT_URL": "https://hc.example/abc"})._heartbeat()
            urlopen.assert_called_once_with("https://hc.example/abc", timeout=10)

    def test_network_error_is_swallowed(self):
        with (
            mock.patch("urllib.request.urlopen", side_effect=OSError("нет сети")),
            self.assertLogs("aiplatform.schedule"),
        ):
            self.scheduler({"AIP_HEARTBEAT_URL": "https://hc.example/abc"})._heartbeat()


class AgentScriptTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.calls = self.tmp / "launchctl.log"
        fake = self.tmp / "launchctl"
        fake.write_text(f'#!/bin/sh\necho "$@" >> "{self.calls}"\n')
        fake.chmod(0o755)
        self.env = {
            **os.environ,
            "AIP_AGENTS_DIR": str(self.tmp / "agents"),
            "AIP_LOG_DIR": str(self.tmp / "logs"),
            "AIP_CHECK_AT": "07:30",
            "LAUNCHCTL": str(fake),
        }

    def agent(self, *args):
        return subprocess.run(
            ["sh", str(PLATFORM / "tools" / "agent.sh"), *args],
            env=self.env,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def test_install_renders_plists_and_uninstall_removes(self):
        done = self.agent("install")
        self.assertEqual(done.returncode, 0, done.stderr)
        agents = self.tmp / "agents"
        server = plistlib.loads((agents / "com.aiplatform.server.plist").read_bytes())
        self.assertEqual(server["ProgramArguments"][:4], ["/usr/bin/make", "-C", str(PLATFORM.resolve()), "run"])
        self.assertEqual(server["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual((server["ThrottleInterval"], server["ExitTimeOut"]), (30, 30))
        self.assertTrue(server["RunAtLoad"])
        self.assertIn("/opt/homebrew/bin", server["EnvironmentVariables"]["PATH"])
        self.assertTrue(server["StandardOutPath"].startswith(str(self.tmp / "logs")))
        check = plistlib.loads((agents / "com.aiplatform.digest-check.plist").read_bytes())
        self.assertEqual(check["StartCalendarInterval"], {"Hour": 7, "Minute": 30})
        self.assertTrue(check["ProgramArguments"][1].endswith("tools/digest_check.py"))
        self.assertNotIn("TOKEN", str(check))  # секреты — не в plist
        uid = os.getuid()
        self.assertIn(f"bootstrap gui/{uid} {agents}/com.aiplatform.server.plist", self.calls.read_text())

        done = self.agent("uninstall")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(list(agents.iterdir()), [])
        self.assertIn(f"bootout gui/{uid}/com.aiplatform.digest-check", self.calls.read_text())

    def test_backup_agent_only_with_backup_dir(self):
        agents = self.tmp / "agents"
        self.assertEqual(self.agent("install").returncode, 0)
        self.assertFalse((agents / "com.aiplatform.backup.plist").exists())
        self.env |= {"AIP_BACKUP_DIR": str(self.tmp / "backups"), "AIP_BACKUP_AT": "04:05"}
        done = self.agent("install")
        self.assertEqual(done.returncode, 0, done.stderr)
        plist = plistlib.loads((agents / "com.aiplatform.backup.plist").read_bytes())
        self.assertEqual(plist["ProgramArguments"][:4], ["/usr/bin/make", "-C", str(PLATFORM.resolve()), "backup"])
        self.assertEqual(plist["StartCalendarInterval"], {"Hour": 4, "Minute": 5})
        self.assertNotIn("AGE", str(plist))  # ключи — в local.mk, не в plist
        self.assertEqual(self.agent("uninstall").returncode, 0)
        self.assertEqual(list(agents.iterdir()), [])

    def test_unknown_command(self):
        self.assertEqual(self.agent("nope").returncode, 2)


if __name__ == "__main__":
    unittest.main()
