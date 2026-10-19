"""Стиль-библия проекта (MASTER_SPEC §5.4): палитра, типографика, движение, субтитры; версии.

По умолчанию — дизайн-система платформы (DEFAULT); LLM (`### task: montage_style`) уточняет под
тему. Каждая версия — `styles/style_NNN.json`, активная — `style_bible.json`; ревизия входит в
хэш сцены: новая ревизия → сцены перерисовываются (§6).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import schemas
from aiplatform.motion import agents

TASK = "montage_style"
FILE = "style_bible.json"
DIR = "styles"
DENSITIES = ("low", "medium", "high")
EASINGS = ("power1.out", "power2.out", "power3.out", "power4.out", "back.out(1.4)", "expo.out", "sine.out",
           "power1.in", "power2.in", "power3.in", "sine.in", "none")  # fmt: skip
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_REV = re.compile(r"^style_(\d{3})$")
DEFAULT = {
    "schema_version": schemas.SCHEMA_VERSION,
    "revision": "style_000",
    "name": "Тёмный технический",
    "palette": dict(agents.DEFAULT_DESIGN["palette"]),
    "font": "system",
    "mono": "sf-mono",
    "spacing": {"unit": 8, "margin": 96},
    "motion": {"enter": "power3.out", "exit": "power2.in", "hold_ease": "none", "enter_s": 0.5, "exit_s": 0.4},
    "captions": {"size": 64, "position": "bottom", "margin": 90, "max_chars": 42},
    "safe_margin_pct": 5.0,
    "logo": {"usage": "нет логотипа"},
    "density": "medium",
    "composition": "карточка слева или по центру, голова автора остаётся видна",
    "iconography": "простые линейные пиктограммы, без клипарта",
    "camera": "статичный кадр, движение — только у элементов",
}
RULES = """Ты — арт-директор YouTube-канала. Задай единый визуальный язык выпуска для моушн-сцен поверх
говорящей головы: палитра (9 цветов #rrggbb: bg, surface, ink, muted, accent, accent2, ok, warn, bad),
шрифт и моноширинный (только из списка), характер движения (easing GSAP из списка, секунды входа и
выхода), стиль субтитров (кегль 48–72, поле снизу), плотность графики (low — редкие крупные
карточки, medium, high — плотнее), правила композиции и пиктограмм. Контраст текста к подложке —
не ниже 4.5:1. Без клипарта, без «стоковых» градиентов, никаких чужих логотипов."""


def prompt(topic: str, beats_summary: str, title: str = "") -> str:
    return (
        f"### task: {TASK}\n{RULES}\n\n"
        f"Ролик: «{title or 'без названия'}». Тема: {topic or 'не определена'}.\nБиты:\n{beats_summary}\n\n"
        f"Шрифты: font — {', '.join(agents.FONTS)}; mono — {', '.join(agents.MONO)}. "
        f"Easing: {', '.join(EASINGS)}.\n"
        'Ответь только JSON: {"name": "<название стиля>", "palette": {"bg": "#rrggbb", …}, "font": "<id>", '
        '"mono": "<id>", "motion": {"enter": "<easing>", "exit": "<easing>", "enter_s": <0.2–0.8>, '
        '"exit_s": <0.2–0.6>}, "captions": {"size": <48–72>, "margin": <60–140>}, '
        f'"density": "<{"|".join(DENSITIES)}>", "composition": "<правило>", "iconography": "<правило>"}}\n'
    )


def _text(value: object, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _num(value: object, lo: float, hi: float, default: float) -> float:
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return default


def clean(data: object, base: dict | None = None) -> dict:
    """Стиль по схеме: кривое — из base (по умолчанию DEFAULT)."""
    base = json.loads(json.dumps(base or DEFAULT))
    src = data if isinstance(data, dict) else {}
    out = base
    out["name"] = _text(src.get("name"), 60) or base["name"]
    raw = src.get("palette") if isinstance(src.get("palette"), dict) else {}
    for key in agents.PALETTE_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and _HEX.match(value.strip()):
            out["palette"][key] = value.strip().lower()
    out["font"] = src.get("font") if src.get("font") in agents.FONTS else base["font"]
    out["mono"] = src.get("mono") if src.get("mono") in agents.MONO else base["mono"]
    motion = src.get("motion") if isinstance(src.get("motion"), dict) else {}
    out["motion"]["enter"] = motion.get("enter") if motion.get("enter") in EASINGS else base["motion"]["enter"]
    out["motion"]["exit"] = motion.get("exit") if motion.get("exit") in EASINGS else base["motion"]["exit"]
    out["motion"]["enter_s"] = round(_num(motion.get("enter_s"), 0.2, 0.8, base["motion"]["enter_s"]), 2)
    out["motion"]["exit_s"] = round(_num(motion.get("exit_s"), 0.2, 0.6, base["motion"]["exit_s"]), 2)
    caps = src.get("captions") if isinstance(src.get("captions"), dict) else {}
    out["captions"]["size"] = int(_num(caps.get("size"), 48, 72, base["captions"]["size"]))
    out["captions"]["margin"] = int(_num(caps.get("margin"), 60, 140, base["captions"]["margin"]))
    out["density"] = src.get("density") if src.get("density") in DENSITIES else base["density"]
    for key in ("composition", "iconography", "camera"):
        out[key] = _text(src.get(key)) or base[key]
    out["schema_version"] = schemas.SCHEMA_VERSION
    return out


def load(folder: Path) -> dict | None:
    try:
        data = json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return clean(data, data) if isinstance(data, dict) else None


def next_revision(folder: Path) -> str:
    n = 0
    for path in (folder / DIR).glob("style_*.json"):
        if found := _REV.match(path.stem):
            n = max(n, int(found[1]))
    return f"style_{n + 1:03d}"


def save(folder: Path, style: dict) -> dict:
    """Новая ревизия: styles/<rev>.json и активная style_bible.json."""
    style = clean(style, style)
    style["revision"] = next_revision(folder)
    schemas.check("style_bible", style)
    (folder / DIR).mkdir(parents=True, exist_ok=True)
    text = json.dumps(style, ensure_ascii=False, indent=2)
    atomic_write(folder / DIR / f"{style['revision']}.json", text)
    atomic_write(folder / FILE, text)
    return style


def css_vars(style: dict) -> str:
    pal = "".join(f"--{k}:{v};" for k, v in style["palette"].items())
    return (
        f":root{{{pal}--font:{agents.FONTS[style['font']]};--mono:{agents.MONO[style['mono']]};"
        f"--unit:{style['spacing']['unit']}px;--margin:{style['spacing']['margin']}px;}}"
    )


def describe(style: dict) -> str:
    """Стиль словами — для раскадровщика."""
    pal = ", ".join(f"{k} {v}" for k, v in style["palette"].items())
    return (
        f"Стиль «{style['name']}». Палитра: {pal}. Шрифт {style['font']}, моно {style['mono']}. "
        f"Движение: вход {style['motion']['enter']} {style['motion']['enter_s']} с, выход {style['motion']['exit']} "
        f"{style['motion']['exit_s']} с. Плотность графики: {style['density']}. Композиция: {style['composition']}. "
        f"Пиктограммы: {style['iconography']}."
    )
