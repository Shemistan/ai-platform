"""Студия разбита на ES-модули по зонам (FE-010): ни один не разрастается обратно в монолит,
локальные импорты указывают на существующие модули и имена, состояние описано в одном месте."""

import pathlib
import re
import unittest

STUDIO = pathlib.Path(__file__).resolve().parent.parent / "aiplatform" / "static" / "studio"
MAX_LINES = 400


def modules():
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(STUDIO.glob("*.js"))}


class StudioModulesTest(unittest.TestCase):
    def test_zones_present_and_small(self):
        mods = modules()
        for name in ("app.js", "state.js", "settings.js", "steps.js", "publish.js"):
            self.assertIn(name, mods)
        for name, text in mods.items():
            with self.subTest(module=name):
                self.assertLessEqual(len(text.splitlines()), MAX_LINES)

    def test_local_imports_resolve(self):
        # import { a, b } from "./x.js" — файл есть, и каждое имя он экспортирует.
        mods = modules()
        for name, text in mods.items():
            for names, target in re.findall(r'import \{([^}]*)\} from "\./([\w-]+\.js)";', text):
                with self.subTest(module=name, target=target):
                    self.assertIn(target, mods)
                    for item in names.split(","):
                        item = re.escape(item.strip().split(" as ")[0])
                        self.assertRegex(mods[target], rf"export (?:async function|function|const|let) {item}\b")

    def test_state_declared_once(self):
        mods = modules()
        declared = [name for name, text in mods.items() if re.search(r"^(?:export )?const state = \{", text, re.M)]
        self.assertEqual(declared, ["state.js"])
        page = (STUDIO / "index.html").read_text(encoding="utf-8")
        self.assertIn('src="app.js"', page.replace("./app.js", "app.js"))
