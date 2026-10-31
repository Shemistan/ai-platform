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
        # Студия разбита на модули (FE-010): toast берёт хотя бы один модуль вкладки.
        def uses_toast(js):
            imported = re.search(r"import \{[^}]*\btoast\b[^}]*\} from \"/ui.js\"", js)
            return bool(imported) and "toast(" in js.split('from "/ui.js"', 1)[1]

        for tab in ("settings", "studio", "channels"):
            modules = [p.read_text(encoding="utf-8") for p in sorted((STATIC / tab).glob("*.js"))]
            with self.subTest(tab=tab):
                self.assertTrue(any(uses_toast(js) for js in modules))

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


class DisclosureTest(unittest.TestCase):
    """DS-016: раскрывающийся блок — один details.disclosure из ui.css."""

    def test_old_variants_gone(self):
        old = r"settings-box|\.advanced summary|\.step-settings.*summary|\.log-wrap|details\.inline|\.editor\[open\]"
        self.assertEqual(offenders(old), [])

    def test_every_details_is_disclosure(self):
        found = []
        for p in sorted(STATIC.rglob("*.html")):
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "<details" in line and "disclosure" not in line:
                    found.append(f"{p.relative_to(STATIC)}:{n}")
        for p in sorted(STATIC.rglob("*.js")):
            text = p.read_text(encoding="utf-8")
            # h("details", {…}) — класс в объекте атрибутов сразу после тега. Меню «⋯» (moreMenu, DS-019) —
            # другой компонент на том же <details>: список действий, а не раскрывающийся блок.
            for m in re.finditer(r'h\(\s*"details",\s*(\{[^}]*\})', text):
                if "disclosure" not in m.group(1) and "more-menu" not in m.group(1):
                    found.append(f"{p.relative_to(STATIC)}:{text.count(chr(10), 0, m.start()) + 1}")
        self.assertEqual(found, [])

    def test_disclosure_states(self):
        ui = (STATIC / "ui.css").read_text(encoding="utf-8")
        rules = ("details.disclosure > summary {", "details.disclosure[open] > summary", "::-webkit-details-marker")
        for rule in (*rules, "disclosure--title"):
            self.assertIn(rule, ui)
        self.assertIn("summary):focus-visible", ui)


class SwitchTest(unittest.TestCase):
    """DS-013: переключатель — один input[role=switch] из ui.css, не растянутый checkbox."""

    def test_no_stretched_checkbox(self):
        # Ширина 34px у флажка — признак старого «растянутого» тумблера; теперь она только в ui.css.
        found = [f for f in offenders(r"width: *34px") if not f.startswith("ui.css:")]
        self.assertEqual(found, [])
        self.assertEqual(offenders(r"\.switch input|toggle-row input\[type"), [])

    def test_switch_component(self):
        ui = (STATIC / "ui.css").read_text(encoding="utf-8")
        self.assertIn('input[type="checkbox"][role="switch"] {', ui)
        self.assertIn("appearance: none", ui)
        for state in (":checked", ":focus-visible", ":disabled"):
            self.assertIn(f'[role="switch"]{state}', ui)

    def test_switches_have_role(self):
        for tab in ("settings", "channels"):
            js = (STATIC / tab / "app.js").read_text(encoding="utf-8")
            with self.subTest(tab=tab):
                self.assertRegex(js, r'type: "checkbox",\s*role: "switch"')


class IconsTest(unittest.TestCase):
    """DS-014: иконки — SVG из icon()/iconButton() в ui.js, а не глифы шрифта в кнопках."""

    # Студия и доска — отдельные DS-017/DS-018 (их метки статуса ✓ ↷ — не кнопки).
    SKIP = ("studio", "board")

    def js(self):
        for p in sorted(STATIC.rglob("*.js")):
            if p.parent.name not in self.SKIP:
                yield p, p.read_text(encoding="utf-8")

    def test_no_glyph_buttons(self):
        found = [
            f"{p.relative_to(STATIC)}:{n}: {line.strip()[:80]}"
            for p, text in self.js()
            for n, line in enumerate(text.splitlines(), 1)
            if not line.lstrip().startswith("//") and re.search(r'"[×+→★✓▶◀↗]"|[★↗]', line)
        ]
        self.assertEqual(found, [])

    def test_icon_buttons_named_and_sized(self):
        ui = (STATIC / "ui.css").read_text(encoding="utf-8")
        rule = re.search(r"\.btn-icon \{([^}]*)\}", ui).group(1)
        self.assertRegex(rule, r"width: 32px")
        self.assertRegex(rule, r"height: 32px")
        self.assertNotIn(".icon-btn", ui)
        # Каждая кнопка-иконка — через iconButton(имя, подпись): подпись уходит в aria-label.
        for p, text in self.js():
            for m in re.finditer(r"iconButton\(\"(\w+)\", *([^,)]+)", text):
                with self.subTest(at=f"{p.relative_to(STATIC)}:{m.group(1)}"):
                    self.assertTrue(m.group(2).strip().strip("`\"'"))
        ui_js = (STATIC / "ui.js").read_text(encoding="utf-8")
        self.assertIn('"aria-hidden": "true"', ui_js)
        self.assertIn('"aria-label": label', ui_js)


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
