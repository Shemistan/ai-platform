"""Дизайн-токены: размеры в CSS берутся только из static/tokens.css (DS-004…DS-007)."""

import pathlib
import re
import unittest

STATIC = pathlib.Path(__file__).resolve().parent.parent / "aiplatform" / "static"


def styles():
    """Все стили, кроме самих токенов: CSS-файлы и <style> внутри страниц."""
    for path in sorted(STATIC.rglob("*.css")):
        if path.name != "tokens.css":
            yield path, path.read_text(encoding="utf-8")
    for path in sorted(STATIC.rglob("*.html")):
        for block in re.findall(r"<style>(.*?)</style>", path.read_text(encoding="utf-8"), re.S):
            yield path, block


def offenders(pattern):
    found = []
    for path, text in styles():
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(pattern, line):
                found.append(f"{path.relative_to(STATIC)}:{n}: {line.strip()}")
    return found


class TokensTest(unittest.TestCase):
    def test_radius_only_from_tokens(self):
        self.assertEqual(offenders(r"border-radius: *[0-9]"), [])

    def test_font_size_only_from_tokens(self):
        # И отдельное свойство, и шорткат font: 13px/1.5 …
        self.assertEqual(offenders(r"font(-size)?: *[0-9]"), [])

    def test_type_scale_in_rem(self):
        tokens = (STATIC / "tokens.css").read_text(encoding="utf-8")
        sizes = re.findall(r"--fs-[\w-]+: *([^;]+);", tokens)
        self.assertEqual(len(sizes), 6)
        self.assertTrue(all(s.endswith("rem") for s in sizes), sizes)

    def test_no_inline_font_size_in_js(self):
        found = [
            f"{p.relative_to(STATIC)}:{n}"
            for p in sorted(STATIC.rglob("*.js"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if "font-size:" in line
        ]
        self.assertEqual(found, [])
