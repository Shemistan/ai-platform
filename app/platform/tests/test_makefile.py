"""Makefile: на старом Python цели падают понятной ошибкой, а не SyntaxError."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent

# Притворяется python3 3.9 из Xcode CLT: проверка версии — провал, версия — 3.9.6.
OLD_PYTHON = """#!/bin/sh
case "$*" in
  *version_info*) exit 1 ;;
  *python_version*) echo 3.9.6 ;;
  *) echo "SyntaxError: invalid syntax" >&2; exit 1 ;;
esac
"""


@unittest.skipUnless(shutil.which("make"), "нет make")
class PythonCheckTest(unittest.TestCase):
    def make(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["make", "-s", "-C", str(SERVICE_DIR), *args], capture_output=True, text=True, timeout=60)

    def test_old_python_stops_with_install_hint(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = Path(tmp) / "python3"
            fake.write_text(OLD_PYTHON)
            fake.chmod(0o755)
            for target in ("demo", "run", "smoke", "test", "check"):
                with self.subTest(target=target):
                    proc = self.make(target, f"PYTHON={fake}")
                    self.assertNotEqual(proc.returncode, 0)
                    self.assertIn("Нужен Python 3.11+, найден 3.9.6", proc.stderr)
                    self.assertIn("brew install python@3.12", proc.stderr)
                    self.assertIn(f"make PYTHON=python3.12 {target}", proc.stderr)
                    self.assertNotIn("SyntaxError", proc.stderr + proc.stdout)

    def test_missing_python_is_reported(self):
        proc = self.make("python-check", "PYTHON=/нет/такого/python")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("не найден", proc.stderr)

    def test_new_python_passes(self):
        proc = self.make("python-check", f"PYTHON={sys.executable}")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main()
