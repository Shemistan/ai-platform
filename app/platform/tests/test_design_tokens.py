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

    def test_no_legacy_button_aliases(self):
        # Старые имена вариантов .primary/.secondary/.link/.danger убраны (DS-018): в CSS их нет,
        # в разметке и JS кнопки только на .btn--*.
        self.assertEqual(offenders(r"\.(?:primary|secondary|link|danger)\b"), [])
        found = [
            f"{p.relative_to(STATIC)}:{n}: {line.strip()}"
            for p in sorted([*STATIC.rglob("*.js"), *STATIC.rglob("*.html")])
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            for cls in re.findall(r"class(?:: |=)\"([^\"]*)\"", line)
            if re.search(r"(?<![\w-])(?:primary|secondary|link|danger)(?![\w-])", cls)
        ]
        self.assertEqual(found, [])

    def test_board_on_tone_classes(self):
        # Цвет карточки доски — от .tone-* (cardTone → toneOf), без своих правил по группам (DS-017).
        css = (STATIC / "board" / "style.css").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"\.board-card\.(?:attention|running|published|uploaded|drafts|failed)\b", css), [])
        self.assertNotRegex(css, r"--(?:warn|warning|info|success)(?:-bg)?\b|--bad-bg")
        js = (STATIC / "board" / "app.js").read_text(encoding="utf-8")
        self.assertIn("board-card tone-${cardTone(p)}", js)

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
