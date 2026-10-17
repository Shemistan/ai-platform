#!/usr/bin/env python3
"""Тесты tools/context_gate.py: python3 tools/test_context_gate.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import context_gate  # noqa: E402


class ContextGateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name
        self.touch("app/platform/aiplatform/studio/pipeline.py", 'STEPS = (Step("text", "Текст"),)\n')
        self.touch("app/platform/aiplatform/studio/service.py", 'SCENARIOS = {"digest": "Дайджест"}\n')
        self.touch("app/platform/aiplatform/server.py")
        self.touch("app/platform/aiplatform/__init__.py")
        self.touch("app/platform/aiplatform/static/board/app.js")
        self.touch("app/narrator/src/narrator/cli.py")
        self.touch("docs/decisions/0001-x.md")

    def touch(self, path, text=""):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(text)

    def context(self, text):
        self.touch(context_gate.CONTEXT, text)

    FULL = "app/platform/ app/narrator/ static/ studio/ server.py board/ cli.py `text` `digest` 0001\n"

    def test_complete_map_passes(self):
        self.context(self.FULL)
        self.assertIsNone(context_gate.check(self.root))

    def test_new_package_without_line_fails(self):
        self.context(self.FULL)
        self.touch("app/platform/aiplatform/billing/__init__.py")
        err = context_gate.check(self.root)
        self.assertIn("модуль платформы: billing/", err)

    def test_new_step_scenario_adr_fail(self):
        self.context(self.FULL)
        self.touch("app/platform/aiplatform/studio/pipeline.py", 'Step("text", "Т"), Step("dub", "Дубляж")\n')
        self.touch("docs/decisions/0002-y.md")
        err = context_gate.check(self.root)
        self.assertIn("шаг студии: `dub`", err)
        self.assertIn("ADR: 0002", err)

    def test_bare_word_is_not_a_step(self):
        self.context(self.FULL.replace("`text`", "text"))
        self.assertIn("шаг студии: `text`", context_gate.check(self.root))

    def test_missing_map_and_limit(self):
        self.assertIn("нет файла", context_gate.check(self.root))
        self.context(self.FULL + "строка\n" * 5)
        self.assertIn("лимите 3", context_gate.check(self.root, limit=3))


if __name__ == "__main__":
    unittest.main()
