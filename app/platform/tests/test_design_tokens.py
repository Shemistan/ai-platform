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

    def test_spacing_only_from_tokens(self):
        # padding/margin/gap: числа в px — только волосяные 1–2px, остальное — --sp-*.
        decl = r"(?<![\w-])(?:padding|margin|gap|row-gap|column-gap)(?:-[a-z-]+)?\s*:[^;}]*"
        self.assertEqual(offenders(decl + r"(?<![\w.-])(?:[3-9]|\d{2,})px"), [])

    def test_one_button_component(self):
        # Кнопки — .btn с вариантами, переключатели — .segmented (DS-007); старых видов нет.
        self.assertEqual(offenders(r"\.(small-btn|tool|add-btn|row-btn|mode-tab|voices|tempo)\b"), [])
        ui = (STATIC / "ui.css").read_text(encoding="utf-8")
        for variant in ("primary", "secondary", "ghost", "danger", "link", "sm", "lg"):
            self.assertIn(f".btn--{variant}", ui)
        for state in (":hover", ":disabled", ":focus-visible"):
            self.assertIn(state, ui)
        used = "\n".join(p.read_text(encoding="utf-8") for p in STATIC.rglob("*.js"))
        used += "\n".join(p.read_text(encoding="utf-8") for p in STATIC.rglob("*.html"))
        self.assertNotIn("small-btn", used)

    def test_channel_remove_is_danger(self):
        js = (STATIC / "channels" / "app.js").read_text(encoding="utf-8")
        line = next(ln for ln in js.splitlines() if "removeChannel(c)" in ln and "Убрать" in ln)
        self.assertIn("btn--danger", line)

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


class ToastTest(unittest.TestCase):
    """DS-009: подтверждение фоновых действий — общий toast() из ui.js."""

    def test_toast_used_in_settings_studio_channels(self):
        for tab in ("settings", "studio", "channels"):
            js = (STATIC / tab / "app.js").read_text(encoding="utf-8")
            with self.subTest(tab=tab):
                self.assertRegex(js, r"import \{[^}]*\btoast\b[^}]*\} from \"/ui.js\"")
                self.assertIn("toast(", js.split('from "/ui.js"', 1)[1])

    def test_toast_layer_from_tokens(self):
        ui = (STATIC / "ui.css").read_text(encoding="utf-8")
        self.assertRegex(ui, r"\.toasts \{[^}]*z-index: var\(--z-toast\)")


class PageHeadTest(unittest.TestCase):
    """DS-015: одна шапка страницы (.page-head) и ширины контейнеров — только из токенов."""

    def test_container_width_from_tokens(self):
        self.assertEqual(offenders(r"max-width: *1\d{3}px"), [])

    def test_one_page_head(self):
        self.assertEqual(offenders(r"^\s*\.top\b"), [])
        for tab in ("photo", "video"):
            html = (STATIC / tab / "index.html").read_text(encoding="utf-8")
            with self.subTest(tab=tab):
                self.assertNotIn('class="top"', html)
                self.assertRegex(html, r'<header class="page-head">\s*<div class="page-title">\s*<h1>')
                self.assertIn('id="model-chip"', html)

    def test_banner_defined_once(self):
        where = offenders(r"^\.banner \{")
        self.assertEqual(len(where), 1, where)
        self.assertTrue(where[0].startswith("ui.css:"), where)


class InlineStylesTest(unittest.TestCase):
    """DS-012: статических inline-стилей и <style> в страницах нет — только классы из CSS."""

    def test_no_style_in_html(self):
        for path in sorted(STATIC.rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            with self.subTest(page=str(path.relative_to(STATIC))):
                self.assertNotIn("style=", text)
                self.assertNotIn("<style>", text)

    def test_js_style_only_dynamic(self):
        # style в h() — только вычисляемый (ширина полосы, позиция маркера): шаблон с ${…}.
        found = [
            f"{p.relative_to(STATIC)}:{n}: {line.strip()}"
            for p in sorted(STATIC.rglob("*.js"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if re.search(r"\bstyle: *(\"|'|`[^`$]*`)", line)
        ]
        self.assertEqual(found, [])
