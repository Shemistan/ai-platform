"""Моушн-вставки поверх смонтированного ролика: LLM выбирает места и тексты, HyperFrames рисует.

Вставка — шаблон (title, lower_third, stat, list, quote) с полями и временем в ролике.
Каждая — своя HTML-композиция HyperFrames (GSAP, прозрачный фон), рендер — прозрачный
MOV (ProRes 4444), наложение — в финальном проходе ffmpeg (render.final_argv).
"""

from __future__ import annotations

import html
import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import runner
from aiplatform.montage import plan, render
from aiplatform.montage.config import MontageConfig

ASSETS = Path(__file__).resolve().parent / "assets"
RULES = ASSETS / "motion-rules.md"
STYLE = ASSETS / "motion" / "style.css"
GSAP = "https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"
FPS = 30
MIN_S, MAX_S, DEFAULT_S = 2.5, 7.0, 4.0
SPACING_S = 25.0  # не чаще одной вставки за столько секунд
EDGE_START_S, EDGE_END_S = 3.0, 5.0
LIMITS = {"title": 40, "kicker": 30, "subtitle": 60, "value": 8, "label": 60, "item": 30, "text": 140, "author": 40}
TEMPLATES = {
    "title": "Заголовок главы",
    "lower_third": "Подпись-название",
    "stat": "Цифра",
    "list": "Список",
    "quote": "Цитата",
}
INSTALL_HINT = "HyperFrames не установлен (нужен Node 22+): make models M=hyperframes в app/platform."


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clean_inserts(data: object, duration: float) -> list[dict]:
    """Вставки из ответа LLM: шаблон и поля по правилам, время в ролике, не теснее SPACING_S."""
    items = data.get("inserts") if isinstance(data, dict) else None
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("template") not in TEMPLATES:
            continue
        try:
            at = float(item.get("at"))
            length = float(item.get("duration") or DEFAULT_S)
        except (TypeError, ValueError):
            continue
        length = min(MAX_S, max(MIN_S, length))
        if at < EDGE_START_S or at + length > duration - EDGE_END_S + 1e-6:
            continue
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else {}
        kind = item["template"]
        clean: dict = {}
        if kind == "title":
            clean = {
                "title": _text(fields.get("title"), LIMITS["title"]),
                "kicker": _text(fields.get("kicker"), LIMITS["kicker"]),
            }
        elif kind == "lower_third":
            clean = {
                "title": _text(fields.get("title"), LIMITS["title"]),
                "subtitle": _text(fields.get("subtitle"), LIMITS["subtitle"]),
            }
        elif kind == "stat":
            clean = {
                "value": _text(fields.get("value"), LIMITS["value"]),
                "label": _text(fields.get("label"), LIMITS["label"]),
            }
        elif kind == "list":
            raw = fields.get("items") if isinstance(fields.get("items"), list) else []
            clean = {"title": _text(fields.get("title"), LIMITS["title"]),
                     "items": [t for t in (_text(x, LIMITS["item"]) for x in raw[:4]) if t]}  # fmt: skip
            if len(clean["items"]) < 2:
                continue
        elif kind == "quote":
            clean = {
                "text": _text(fields.get("text"), LIMITS["text"]),
                "author": _text(fields.get("author"), LIMITS["author"]),
            }
        main = clean.get("title") or clean.get("value") or clean.get("text")
        if not main:
            continue
        out.append({"at": round(at, 2), "duration": round(length, 2), "template": kind, "fields": clean})
    out.sort(key=lambda x: x["at"])
    spaced: list[dict] = []
    for item in out:
        if not spaced or item["at"] >= spaced[-1]["at"] + SPACING_S:
            spaced.append(item)
    return spaced


def prompt(packed: str, rules: str, duration: float) -> str:
    return (
        "### task: montage_motion\n"
        f"{rules}\n\n"
        f"Длительность ролика: {duration:.0f} с. Время в расшифровке — секунды ролика.\n"
        "Шаблоны и поля:\n"
        '- title: {"title", "kicker"} — заголовок главы, kicker — надпись над ним (можно пусто)\n'
        '- lower_third: {"title", "subtitle"} — название и что это\n'
        '- stat: {"value", "label"} — цифра и что она значит\n'
        '- list: {"title", "items": [2–4 пункта]}\n'
        '- quote: {"text", "author"}\n'
        'Ответь только JSON: {"inserts": [{"at": <секунда начала>, "duration": <3–6>, "template": "<шаблон>", '
        '"fields": {…}}]}\n\n'
        f"Расшифровка:\n{packed}\n"
    )


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _body(kind: str, f: dict) -> tuple[str, str]:
    """Разметка карточки и анимация (строки GSAP) для шаблона."""
    if kind == "title":
        kicker = f'<div class="kicker">{_e(f["kicker"])}</div>' if f.get("kicker") else ""
        card = f'<div class="card pos-center">{kicker}<div class="headline">{_e(f["title"])}</div></div>'
        anim = 'tl.from(".card", {scale: 0.92, opacity: 0, duration: 0.6, ease: "back.out(1.6)"}, 0.1);'
    elif kind == "lower_third":
        sub = f'<div class="sub">{_e(f["subtitle"])}</div>' if f.get("subtitle") else ""
        title = f'<div class="headline">{_e(f["title"])}</div>'
        card = f'<div class="card pos-lower"><div class="bar"></div>{title}{sub}</div>'
        anim = (
            'tl.from(".card", {x: -80, opacity: 0, duration: 0.55, ease: "power3.out"}, 0.1);'
            'tl.from(".bar", {scaleY: 0, transformOrigin: "50% 100%", duration: 0.4, ease: "power2.out"}, 0.3);'
        )
    elif kind == "stat":
        card = (
            f'<div class="card pos-left"><div class="value">{_e(f["value"])}</div>'
            f'<div class="label">{_e(f["label"])}</div></div>'
        )
        anim = (
            'tl.from(".card", {y: 60, opacity: 0, duration: 0.55, ease: "power3.out"}, 0.1);'
            'tl.from(".value", {scale: 0.6, opacity: 0, duration: 0.7, ease: "back.out(2)"}, 0.3);'
        )
    elif kind == "list":
        items = "".join(f"<li><b>{n:02d}</b><span>{_e(t)}</span></li>" for n, t in enumerate(f["items"], 1))
        kicker = f'<div class="kicker">{_e(f["title"])}</div>'
        card = f'<div class="card pos-left">{kicker}<ol class="items">{items}</ol></div>'
        anim = (
            'tl.from(".card", {y: 50, opacity: 0, duration: 0.5, ease: "power3.out"}, 0.1);'
            'tl.from(".items li", {x: -40, opacity: 0, duration: 0.4, stagger: 0.35, ease: "power2.out"}, 0.4);'
        )
    else:
        author = f'<div class="author">— {_e(f["author"])}</div>' if f.get("author") else ""
        card = f'<div class="card pos-center"><div class="quote">«{_e(f["text"])}»</div>{author}</div>'
        anim = 'tl.from(".card", {y: 40, opacity: 0, duration: 0.7, ease: "power2.out"}, 0.1);'
    return card, anim


def composition(insert: dict, style: str) -> str:
    """HTML-композиция HyperFrames для одной вставки: корень #stage, таймлайн GSAP в window.__timelines."""
    d = insert["duration"]
    card, anim = _body(insert["template"], insert["fields"])
    center = 'gsap.set(".pos-center", {xPercent: -50, yPercent: -50});' if "pos-center" in card else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<script src="{GSAP}"></script>
<style>{style}</style>
</head><body>
<div id="stage" data-composition-id="insert" data-start="0" data-duration="{d}" data-width="1920" data-height="1080">
  <div class="clip" data-start="0" data-duration="{d}" data-track-index="0">{card}</div>
</div>
<script>
{center}
const tl = gsap.timeline({{ paused: true }});
{anim}
tl.to(".card", {{opacity: 0, y: -24, duration: 0.45, ease: "power2.in"}}, {max(0.0, d - 0.55):.2f});
window.__timelines = window.__timelines || {{}};
window.__timelines.insert = tl;
</script>
</body></html>
"""


def render_argv(config: MontageConfig, folder: Path) -> list[str]:
    return [*config.hyperframes_cmd, "render", "--composition", "index.html", "--format", "mov", "--fps", str(FPS),
            "--output", str(folder / "insert.mov"), "--quiet"]  # fmt: skip


def run(
    config: MontageConfig,
    project: dict,
    folder: Path,
    out_words: list[dict],
    duration: float,
    cancel: threading.Event,
    *,
    ask: Callable[[str], object],
    note: Callable[[str], None],
    progress: Callable[[str, float], None],
) -> tuple[list[render.Overlay], dict]:
    """Вставки для ролика: (наложения, сводка для карточки). Нет HyperFrames — пусто и причина."""
    if not config.hyperframes_available():
        return [], {"skipped": INSTALL_HINT}
    progress("ИИ ищет, где нужны вставки", 0)
    packed = plan.pack([{"w": w["w"], "s": w["s"], "e": w["e"]} for w in out_words])
    inserts = clean_inserts(ask(prompt(packed, RULES.read_text(encoding="utf-8"), duration)), duration)
    root = folder / "motion"
    root.mkdir(exist_ok=True)
    for old in root.glob("*/insert.mov"):
        old.unlink(missing_ok=True)
    if not inserts:
        return [], {"inserts": 0, "skipped": "ИИ не нашёл мест, где вставка добавит смысла."}
    style = STYLE.read_text(encoding="utf-8")
    overlays: list[render.Overlay] = []
    failed = 0
    for n, item in enumerate(inserts, 1):
        progress(f"вставка {n} из {len(inserts)}", 10 + 80 * (n - 1) / len(inserts))
        target = root / f"{n:02d}"
        target.mkdir(exist_ok=True)
        (target / "index.html").write_text(composition(item, style), encoding="utf-8")
        (target / "insert.json").write_text(json.dumps(item, ensure_ascii=False), encoding="utf-8")
        try:
            runner.run(render_argv(config, target), lambda line: note(line) if line.strip() else None, cancel,
                       cwd=target, missing_hint=INSTALL_HINT, kind="render")  # fmt: skip
        except runner.RunFailed as exc:
            failed += 1
            note(f"Вставка {n} не отрендерилась: {exc}")
            continue
        mov = target / "insert.mov"
        if mov.is_file() and mov.stat().st_size:
            overlays.append(render.Overlay(mov, item["at"], item["duration"]))
    info = {"inserts": len(overlays), "items": [{k: i[k] for k in ("at", "template", "fields")} for i in inserts]}
    if failed:
        info["failed"] = failed
    return overlays, info


_TAGS = re.compile(r"<[^>]+>")


def first_text(document: str) -> str:
    """Текст первой карточки композиции — для фейкового рендера."""
    body = document.split('class="clip"', 1)[-1].split(">", 1)[-1]
    return " ".join(html.unescape(_TAGS.sub(" ", body.split("</div>\n</div>", 1)[0])).split())[:60]
