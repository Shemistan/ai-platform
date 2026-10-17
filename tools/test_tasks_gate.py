#!/usr/bin/env python3
"""Тесты tools/tasks_gate.py: python3 tools/test_tasks_gate.py"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tasks_gate  # noqa: E402

HEAD = """---
тип: task
id: {id}
направление: {dir}
статус: {st}
приоритет: P0
размер: S
обновлено: 2026-10-17
источник: тест
связано: {links}
сделано: {done}
pr: {pr}
---

# {title}
"""


class TasksGateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        os.makedirs(os.path.join(self.root, "backlog", "tasks"))

    def tearDown(self):
        self.tmp.cleanup()

    def put(self, name, tid="BE-001", st="бэклог", links="нет", done="нет", pr="нет",
            title="Задача"):
        d = tid.split("-")[0]
        with open(os.path.join(self.root, "backlog", "tasks", name), "w",
                  encoding="utf-8") as f:
            f.write(HEAD.format(id=tid, dir=d, st=st, links=links, done=done, pr=pr,
                                title=title))

    def errors(self):
        return [m for _, m in tasks_gate.check(tasks_gate.collect(self.root))]

    def test_valid(self):
        self.put("BE-001-x.md")
        self.put("SEC-001-y.md", tid="SEC-001", st="сделано", done="2026-10-17",
                 pr="20", links="BE-001")
        self.assertEqual(self.errors(), [])

    def test_done_requires_date_and_pr(self):
        self.put("BE-001-x.md", st="сделано")
        errs = " ".join(self.errors())
        self.assertIn("сделано: YYYY-MM-DD", errs)
        self.assertIn("pr:", errs)

    def test_bad_link_dup_and_filename(self):
        self.put("BE-001-x.md", links="BE-404")
        self.put("BE-001-z.md")
        self.put("wrong.md", tid="BE-002")
        errs = " ".join(self.errors())
        self.assertIn("BE-404", errs)
        self.assertIn("не уникален", errs)
        self.assertIn("имя файла", errs)

    def test_unknown_direction_and_status(self):
        self.put("ZZ-001-x.md", tid="ZZ-001", st="готово")
        errs = " ".join(self.errors())
        self.assertIn("неизвестное направление", errs)
        self.assertIn("'статус'", errs)

    def test_no_backlog_dir_is_skipped(self):
        empty = tempfile.mkdtemp(dir=self.root)
        self.assertEqual(tasks_gate.main(["gate", empty]), 0)

    def test_regenerate_index_and_changelog(self):
        self.put("BE-001-x.md", title="Открытая")
        self.put("BE-002-y.md", tid="BE-002", st="сделано", done="2026-10-17",
                 pr="abc1234", title="Готовая")
        for rel, block in (("backlog/README.md", "tasks-index"),
                           ("backlog/CHANGELOG.md", "tasks-done")):
            with open(os.path.join(self.root, rel), "w", encoding="utf-8") as f:
                f.write("<!-- gate:begin %s -->\n<!-- gate:end %s -->\n" % (block, block))
        tasks = tasks_gate.collect(self.root)
        self.assertEqual(len(tasks_gate.regenerate(self.root, tasks)), 2)
        with open(os.path.join(self.root, "backlog/README.md"), encoding="utf-8") as f:
            index = f.read()
        with open(os.path.join(self.root, "backlog/CHANGELOG.md"), encoding="utf-8") as f:
            log = f.read()
        self.assertIn("Открытая", index)
        self.assertNotIn("Готовая", index)
        self.assertIn("2026-10-17 · [BE-002](tasks/BE-002-y.md) Готовая · abc1234", log)


if __name__ == "__main__":
    unittest.main(verbosity=0)
