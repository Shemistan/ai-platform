"""Ответы фейкового моста годны по схемам contracts/bridge/ (QA-004).

Те же схемы сверяет с настоящим `narrate bridge` тест app/narrator/tests/test_platform_contract.py:
поменял форму ответа в фейке без правки схемы — падает этот тест, а движок с платформой не
расходятся молча.
"""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiplatform.core.narrator import FAKE

CONTRACTS = Path(__file__).resolve().parents[3] / "contracts" / "bridge"


def _load_schema():
    spec = importlib.util.spec_from_file_location("bridge_schema", CONTRACTS / "schema.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


schema = _load_schema()


class FakeBridgeContractTest(unittest.TestCase):
    def setUp(self):
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        self.env = os.environ | {"FAKE_NARRATOR_HOME": home.name, "FAKE_NARRATOR_DELAY": "0"}

    def bridge(self, *args, stdin=""):
        run = subprocess.run(
            [sys.executable, str(FAKE), "bridge", *args],
            input=stdin,
            capture_output=True,
            text=True,
            env=self.env,
            timeout=60,
        )
        self.assertEqual(run.returncode, 0, (args, run.stdout, run.stderr))
        return json.loads(run.stdout)

    def test_every_answer_matches_schema(self):
        article_id = self.bridge("library")["articles"][0]["id"]
        review_id = self.bridge("reviews")["reviews"][0]["id"]
        new = json.dumps({"title": "Очереди", "text": "Брокер — не база."})
        calls = {
            "version": ((), ""),
            "engines": ((), ""),
            "voices": ((), ""),
            "models": ((), ""),
            "preview": ((), "Go 1.26 вышел 8 октября."),
            "split": ((), "# А\n\nтекст\n\n# Б\n\nтекст\n"),
            "library": ((), ""),
            "article": ((article_id,), ""),
            "article-save": ((article_id,), json.dumps({"summary": "О брокерах."})),
            "article-new": ((), new),
            "fetch": (("https://example.dev/q",), ""),
            "sources": ((), ""),
            "failed": ((), ""),
            "top": ((), ""),
            "issue": ((), ""),
            "reviews": ((), ""),
            "review": ((review_id,), ""),
            "bot": ((), ""),
            "subscribers": ((), ""),
            "unsent": ((), ""),
        }
        self.assertEqual(set(calls), set(schema.commands()), "схема на каждую команду моста и наоборот")
        for name, (args, stdin) in calls.items():
            with self.subTest(name):
                problems = schema.check(name, self.bridge(name, *args, stdin=stdin))
                self.assertEqual(problems, [], f"фейк разошёлся со схемой contracts/bridge/{name}.schema.json")


class SchemaCheckTest(unittest.TestCase):
    def test_missing_key_and_wrong_type_are_reported(self):
        sch = {
            "type": "object",
            "required": ["id", "items"],
            "properties": {"id": {"type": "string"}, "items": {"type": "array", "items": {"type": "number"}}},
        }
        self.assertEqual(schema.errors({"id": "a", "items": [1, 2.5], "extra": True}, sch), [])
        self.assertEqual(
            schema.errors({"items": [1, "x"]}, sch), ["$: нет ключа 'id'", "$.items[1]: ждали number, пришло str"]
        )
        self.assertEqual(schema.errors(True, {"type": "number"}), ["$: ждали number, пришло bool"])


if __name__ == "__main__":
    unittest.main()
