"""Команда агентов «Моушна»: у каждой роли свой промпт, формат ответа и чистка.

editor   — редактор смысла: где во всей расшифровке нужна вставка (моменты по индексам слов);
designer — дизайнер: один визуальный язык выпуска (палитра, шрифты, персонажи и объекты);
director — режиссёр: раскадровка сцены битами, привязанными к словам автора;
expert   — технический редактор: правда о предмете — ok / fix (своя раскадровка) / drop;
animator — аниматор: html/css/js сцены в каркасе HyperFrames (composition).

Каждая роль — отдельный вызов LLM (`ask`), ответ — JSON; всё, что пришло от модели, проходит
чистку здесь: индексы в границах, длины, цвета, код аниматора без сети и таймеров.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from aiplatform.montage import plan
from aiplatform.motion import scenes

ASSETS = Path(__file__).resolve().parent / "assets"
GSAP = "https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"
ROLES = {
    "editor": "Редактор смысла",
    "designer": "Дизайнер",
    "director": "Режиссёр",
    "expert": "Технический редактор",
    "animator": "Аниматор",
}
KINDS = ("mechanism", "process", "structure", "comparison", "number")
LAYOUTS = tuple(scenes.LAYOUTS)
MIN_SCENE_S, MAX_SCENE_S = 4.0, 25.0
MIN_GAP_S = 30.0  # между сценами
LEAD_S = 0.3  # сцена начинается чуть раньше первого слова
MIN_SCORE = 5
MAX_BEATS = 8
LOGICAL_W = 1920  # логический кадр аниматора; рендер масштабирует под настоящий размер
FONTS = {
    "system": "-apple-system, 'SF Pro Display', 'Helvetica Neue', Arial, sans-serif",
    "rounded": "'SF Pro Rounded', ui-rounded, -apple-system, 'Helvetica Neue', sans-serif",
    "serif": "'New York', ui-serif, Georgia, 'Times New Roman', serif",
    "condensed": "'Avenir Next Condensed', 'Helvetica Neue', Arial Narrow, sans-serif",
}
MONO = {
    "sf-mono": "'SF Mono', ui-monospace, Menlo, monospace",
    "menlo": "Menlo, Monaco, 'Courier New', monospace",
}
PALETTE_KEYS = ("bg", "surface", "ink", "muted", "accent", "accent2", "ok", "warn", "bad")
DEFAULT_DESIGN = {
    "name": "Тёмный технический",
    "palette": {
        "bg": "#0f1420",
        "surface": "#1a2233",
        "ink": "#eef2f8",
        "muted": "#8a96ab",
        "accent": "#4fc3f7",
        "accent2": "#ffb454",
        "ok": "#5fd38d",
        "warn": "#ffd166",
        "bad": "#ff6b6b",
    },  # fmt: skip
    "font": "system",
    "mono": "sf-mono",
    "background": "тёмный фон с тонкой сеткой точек",
    "panel": "полупрозрачная подложка var(--surface) 88%, скругление 28px, мягкая тень",
    "motion": "уверенные выходы power3.out, контакт — короткий back.out, уход power2.in",
    "characters": [],
}
LIMITS = {"title": 60, "concept": 300, "why": 200, "action": 240, "text": 40, "line": 300, "look": 600}
CODE_LIMITS = {"html": 60_000, "css": 30_000, "js": 30_000}
# Код аниматора: без сети, без своих скриптов, без времени вне таймлайна (рендер листает кадры как хочет).
FORBIDDEN = (
    (re.compile(r"<\s*/?\s*script|<!--", re.I), "<script>"),
    (re.compile(r"<\s*(iframe|object|embed|link|meta|base)\b", re.I), "внешний тег"),
    (re.compile(r"\b(?:https?:)?//[a-z0-9.-]+\.[a-z]{2,}", re.I), "внешняя ссылка"),
    (re.compile(r"@import", re.I), "@import"),
    (re.compile(r"\b(fetch|XMLHttpRequest|WebSocket|EventSource|importScripts|navigator\.sendBeacon)\b"), "сеть"),
    (re.compile(r"\bimport\s*\("), "import()"),
    (re.compile(r"\beval\s*\(|\bnew\s+Function\b"), "eval"),
    (re.compile(r"\b(setTimeout|setInterval|requestAnimationFrame)\b"), "таймер"),
    (re.compile(r"Math\.random"), "Math.random"),
    (re.compile(r"\.(play|resume|restart)\s*\("), "свой запуск таймлайна"),
    (re.compile(r"\bjavascript:", re.I), "javascript:"),
)
_HANDLER = re.compile(r"\son[a-z]+\s*=", re.I)  # onload= в разметке
_SVG_NS = re.compile(r"https?://www\.w3\.org/[\w/.-]*")  # xmlns SVG — не сеть
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_ID = re.compile(r"^[a-z][a-z0-9_-]{0,23}$")


class AgentError(ValueError):
    """Ответ роли не годится: текст — для журнала и повторной попытки аниматора."""


def rules(name: str) -> str:
    return (ASSETS / f"{name}.md").read_text(encoding="utf-8")


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _int(value: object) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _header(task: str, role: str, title: str, brief: str) -> str:
    head = f"### task: {task}\n{rules('craft')}\n\n{rules(role)}\n\n"
    head += f"Ролик: «{title}».\n" if title else ""
    head += f"Пожелания автора: {brief}\n" if brief else ""
    return head


# ---------- редактор смысла ----------


def editor_prompt(words: list[dict], duration: float, max_scenes: int, title: str = "", brief: str = "") -> str:
    return (
        _header("motion_moments", "editor", title, brief)
        + f"\nДлительность ролика: {scenes.clock(duration)} ({duration:.0f} с). Нужно не больше {max_scenes} "
        f"моментов, не ближе {MIN_GAP_S:.0f} с друг к другу; лучше меньше, но сильных.\n"
        "Строки расшифровки: `[индекс первого слова] секунда текст фразы`.\n"
        'Ответь только JSON: {"topic": "<о чём ролик, одна фраза>", "moments": [{"from": <индекс слова>, '
        '"to": <индекс слова>, "title": "<2–5 слов>", "concept": "<что именно показать: механизм, участники, '
        f'действие>", "why": "<зачем зрителю картинка>", "kind": "<{"|".join(KINDS)}>", "score": <1–10>}}]}}\n\n'
        f"Расшифровка:\n{plan.pack(words)}\n"
    )


def clean_moments(data: object, words: list[dict], duration: float, max_scenes: int) -> tuple[str, list[dict]]:
    """(тема, моменты): индексы в границах, польза ≥ MIN_SCORE, лучшие — не теснее MIN_GAP_S, по времени."""
    topic = _text(data.get("topic"), LIMITS["line"]) if isinstance(data, dict) else ""
    items = data.get("moments") if isinstance(data, dict) else None
    out: list[dict] = []
    n = len(words)
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        a, b = _int(item.get("from")), _int(item.get("to"))
        score = _int(item.get("score")) or 0
        if a is None or b is None or not 0 <= a <= b < n or score < MIN_SCORE:
            continue
        title = _text(item.get("title"), LIMITS["title"])
        concept = _text(item.get("concept"), LIMITS["concept"])
        if not title or not concept:
            continue
        start = words[a]["s"]
        if start < 1.0 or words[b]["e"] > duration + 1e-6:
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else "mechanism"
        out.append({"from": a, "to": b, "title": title, "concept": concept, "kind": kind, "score": min(score, 10),
                    "why": _text(item.get("why"), LIMITS["why"]), "at": round(start, 2)})  # fmt: skip
    picked: list[dict] = []
    for item in sorted(out, key=lambda m: (-m["score"], m["at"])):
        if len(picked) >= max_scenes:
            break
        if all(abs(item["at"] - p["at"]) >= MIN_GAP_S for p in picked):
            picked.append(item)
    return topic, sorted(picked, key=lambda m: m["at"])


# ---------- дизайнер ----------


def designer_prompt(topic: str, moments: list[dict], title: str = "", brief: str = "") -> str:
    listing = "\n".join(f"- {m['title']}: {m['concept']}" for m in moments)
    fonts = ", ".join(FONTS)
    monos = ", ".join(MONO)
    return (
        _header("motion_design", "designer", title, brief)
        + f"\nТема ролика: {topic or 'не определена'}.\nСцены выпуска:\n{listing}\n\n"
        f"Шрифты (только эти id): font — {fonts}; mono — {monos}.\n"
        'Ответь только JSON: {"name": "<название стиля>", "palette": {'
        + ", ".join(f'"{k}": "#rrggbb"' for k in PALETTE_KEYS)
        + '}, "font": "<id>", "mono": "<id>", "background": "<фон сцен на весь кадр>", '
        '"panel": "<подложка половины кадра и нижней трети>", "motion": "<характер движения>", '
        '"characters": [{"id": "<латиница>", "name": "<имя>", "look": "<как выглядит и как нарисовать в SVG>"}]}\n'
    )


def clean_design(data: object) -> dict:
    """Язык выпуска: недостающее и кривое — из DEFAULT_DESIGN; до 8 персонажей и объектов."""
    src = data if isinstance(data, dict) else {}
    palette = dict(DEFAULT_DESIGN["palette"])
    raw = src.get("palette") if isinstance(src.get("palette"), dict) else {}
    for key in PALETTE_KEYS:
        if isinstance(raw.get(key), str) and _HEX.match(raw[key].strip()):
            palette[key] = raw[key].strip().lower()
    characters = []
    for item in src.get("characters") if isinstance(src.get("characters"), list) else []:
        if not isinstance(item, dict):
            continue
        cid = str(item.get("id") or "").strip().lower()
        look = _text(item.get("look"), LIMITS["look"])
        if _ID.match(cid) and look and all(c["id"] != cid for c in characters):
            characters.append({"id": cid, "name": _text(item.get("name"), LIMITS["title"]) or cid, "look": look})
    out = {
        "name": _text(src.get("name"), LIMITS["title"]) or DEFAULT_DESIGN["name"],
        "palette": palette,
        "font": src.get("font") if src.get("font") in FONTS else DEFAULT_DESIGN["font"],
        "mono": src.get("mono") if src.get("mono") in MONO else DEFAULT_DESIGN["mono"],
        "characters": characters[:8],
    }
    for key in ("background", "panel", "motion"):
        out[key] = _text(src.get(key), LIMITS["line"]) or DEFAULT_DESIGN[key]
    return out


def design_text(design: dict) -> str:
    """Язык выпуска словами — для режиссёра, техреда и аниматора."""
    lines = [
        f"Стиль «{design['name']}». Палитра: " + ", ".join(f"{k} {v}" for k, v in design["palette"].items()),
        f"Фон сцен на весь кадр: {design['background']}.",
        f"Подложка половины кадра и нижней трети: {design['panel']}.",
        f"Движение: {design['motion']}.",
    ]
    if design["characters"]:
        lines.append("Персонажи и объекты выпуска:")
        lines += [f"- {c['id']} ({c['name']}): {c['look']}" for c in design["characters"]]
    return "\n".join(lines)


# ---------- режиссёр и техред ----------

STORYBOARD_FORMAT = (
    '{"title": "<2–5 слов>", "layout": "<' + "|".join(LAYOUTS) + '>", "idea": "<нить сцены>", '
    '"from": <индекс слова начала>, "to": <индекс слова, где мысль закончилась>, "hold": <0.5–2, с после>, '
    '"characters": ["<id>"], "camera": "<для full, иначе пусто>", "enter": "<как появляется>", '
    '"exit": "<как уходит>", "beats": [{"word": <индекс слова>, "action": "<что происходит на экране>", '
    '"text": "<надпись до 40 знаков или пусто>"}]}'
)


def _fragment(words: list[dict], moment: dict, pad: int = 40) -> str:
    part = scenes.window(words, moment["from"], moment["to"], pad)
    return plan.pack([{k: w[k] for k in ("w", "s", "e")} for w in part], offset=part[0]["i"]) if part else ""


def director_prompt(words: list[dict], moment: dict, design: dict, topic: str, *, feedback: str = "",
                    current: dict | None = None, title: str = "", brief: str = "") -> str:  # fmt: skip
    text = (
        _header("motion_storyboard", "director", title, brief) + f"\nТема ролика: {topic}.\n{design_text(design)}\n\n"
        f"Момент: «{moment['title']}» — {moment['concept']}"
        + (f" (зачем: {moment['why']})" if moment.get("why") else "")
        + f"\nАвтор объясняет это со слова [{moment['from']}] до [{moment['to']}]. "
        "Строки ниже: `[индекс первого слова фразы] секунда текст`; индекс слова внутри фразы — по счёту "
        "от первого.\n"
    )
    if current and feedback:
        text += (
            f"\nПрошлая раскадровка:\n{json.dumps(current, ensure_ascii=False)}\n"
            f"Замечание автора — переделай сцену с учётом него:\n{feedback}\n"
        )
    elif feedback:
        text += f"\nПросьба автора к этой сцене: {feedback}\n"
    return (
        text + f"\nОтветь только JSON: {STORYBOARD_FORMAT}\n\nРасшифровка вокруг момента:\n{_fragment(words, moment)}\n"
    )


def clean_storyboard(data: object, words: list[dict], moment: dict, design: dict) -> dict:
    """Раскадровка: формат, слова в окне момента, ≥2 битов по времени; время сцены в секундах ролика."""
    if not isinstance(data, dict):
        raise AgentError("раскадровка — не объект")
    n = len(words)
    lo, hi = max(0, moment["from"] - 60), min(n - 1, moment["to"] + 60)
    a, b = _int(data.get("from")), _int(data.get("to"))
    a = a if a is not None and lo <= a <= hi else moment["from"]
    b = b if b is not None and a <= b <= hi else max(a, moment["to"])
    beats = []
    for item in data.get("beats") if isinstance(data.get("beats"), list) else []:
        if not isinstance(item, dict):
            continue
        word = _int(item.get("word"))
        action = _text(item.get("action"), LIMITS["action"])
        if word is None or not lo <= word <= hi or not action:
            continue
        beats.append({"word": word, "action": action, "text": _text(item.get("text"), LIMITS["text"])})
    beats.sort(key=lambda x: x["word"])
    if len(beats) < 2:
        raise AgentError("в раскадровке меньше двух битов с действием")
    beats = beats[:MAX_BEATS]
    a = min(a, beats[0]["word"])
    at = max(0.0, words[a]["s"] - LEAD_S)
    try:
        hold = min(2.0, max(0.5, float(data.get("hold") or 1.0)))
    except (TypeError, ValueError):
        hold = 1.0
    end = max(words[b]["e"], words[beats[-1]["word"]]["s"] + 1.5) + hold
    end = min(max(end, at + MIN_SCENE_S), at + MAX_SCENE_S)
    beats = [x for x in beats if words[x["word"]]["s"] - at < end - at - 0.8]
    if len(beats) < 2:
        raise AgentError("биты не помещаются в сцену")
    known = {c["id"] for c in design.get("characters", [])}
    chars = [c for c in data.get("characters") or [] if isinstance(c, str) and c in known]
    layout = data.get("layout") if data.get("layout") in LAYOUTS else "side"
    return {
        "title": _text(data.get("title"), LIMITS["title"]) or moment["title"],
        "layout": layout,
        "idea": _text(data.get("idea"), LIMITS["line"]),
        "from": a,
        "to": b,
        "at": round(at, 3),
        "end": round(end, 3),
        "characters": chars,
        "camera": _text(data.get("camera"), LIMITS["line"]) if layout == "full" else "",
        "enter": _text(data.get("enter"), LIMITS["line"]),
        "exit": _text(data.get("exit"), LIMITS["line"]),
        "beats": [{**x, "t": round(max(0.0, words[x["word"]]["s"] - at), 2)} for x in beats],
    }


def expert_prompt(words: list[dict], moment: dict, board: dict, topic: str, title: str = "") -> str:
    shown = {k: board[k] for k in ("title", "layout", "idea", "from", "to", "characters", "camera", "enter", "exit")}
    shown["beats"] = [{k: x[k] for k in ("word", "action", "text")} for x in board["beats"]]
    return (
        _header("motion_review", "expert", title, "")
        + f"\nТема ролика: {topic}. Момент: «{moment['title']}» — {moment['concept']}\n"
        f"Раскадровка режиссёра:\n{json.dumps(shown, ensure_ascii=False)}\n\n"
        'Ответь только JSON: {"verdict": "ok|fix|drop", "issues": ["<что не так>"], '
        f'"reason": "<коротко>", "storyboard": <при fix — исправленная целиком: {STORYBOARD_FORMAT}>}}\n\n'
        f"Расшифровка вокруг момента:\n{_fragment(words, moment)}\n"
    )


def apply_review(data: object, words: list[dict], moment: dict, board: dict, design: dict) -> tuple[str, dict, list]:
    """(вердикт, раскадровка, замечания). Исправление, которое не прошло чистку, — остаётся прежняя."""
    src = data if isinstance(data, dict) else {}
    verdict = src.get("verdict") if src.get("verdict") in ("ok", "fix", "drop") else "ok"
    issues = [_text(x, LIMITS["line"]) for x in src.get("issues") or [] if isinstance(x, str) and x.strip()][:8]
    if verdict == "drop":
        reason = _text(src.get("reason"), LIMITS["line"]) or (issues[0] if issues else "техред снял сцену")
        return "drop", board, [reason, *issues]
    if verdict == "fix":
        try:
            return "fix", clean_storyboard(src.get("storyboard"), words, moment, design), issues
        except AgentError as exc:
            return "ok", board, [*issues, f"исправление не принято: {exc}"]
    return "ok", board, issues


# ---------- аниматор и каркас ----------


def frame_size(width: int, height: int, mode: str) -> tuple[int, int]:
    """Размер рендера: «1080» — 1920 по длинной стороне 16:9 (или вертикаль 1080×1920), «source» — как у исходника."""
    width, height = (width or 1920), (height or 1080)
    if mode == "source":
        w, h = width, height
    else:
        scale = 1920 / max(width, height) if max(width, height) > 1920 else 1.0
        w, h = width * scale, height * scale
    return int(round(w / 2) * 2), int(round(h / 2) * 2)


def zone(layout: str, lw: int, lh: int) -> tuple[int, int, int, int]:
    """Зона сцены в логическом кадре: x, y, ширина, высота."""
    if layout == "full":
        return 0, 0, lw, lh
    if layout == "lower":
        return round(lw * 0.05), round(lh * 0.66), round(lw * 0.9), round(lh * 0.28)
    return round(lw * 0.5), round(lh * 0.07), round(lw * 0.46), round(lh * 0.86)


def animator_prompt(board: dict, design: dict, topic: str, size: tuple[int, int], *, error: str = "",
                    previous: dict | None = None) -> str:  # fmt: skip
    lw, lh = LOGICAL_W, round(LOGICAL_W * size[1] / size[0])
    x, y, zw, zh = zone(board["layout"], lw, lh)
    d = board["end"] - board["at"]
    beats = [{"t": b["t"], "action": b["action"], "text": b["text"]} for b in board["beats"]]
    chars = [c for c in design["characters"] if c["id"] in board["characters"]] or design["characters"]
    text = (
        f"### task: motion_animate\n{rules('craft')}\n\n{rules('animator')}\n\n"
        f"Тема ролика: {topic}.\n{design_text({**design, 'characters': chars})}\n\n"
        f"Сцена «{board['title']}», формат {board['layout']} ({scenes.LAYOUTS[board['layout']]}). "
        f"Длительность D = {d:.2f} с. Логический кадр {lw}×{lh}; зона сцены ZW×ZH = {zw}×{zh} "
        f"(в кадре с x={x}, y={y}).\n"
        f"Нить: {board['idea']}\nВход: {board['enter']}\nВыход: {board['exit']}\n"
        + (f"Камера: {board['camera']}\n" if board["camera"] else "")
        + f"Биты (BEATS):\n{json.dumps(beats, ensure_ascii=False, indent=1)}\n"
    )
    if error and previous:
        text += (
            f"\nПрошлая версия кода не прошла: {error}\nИсправь её (верни все три куска целиком):\n"
            f"{json.dumps(previous, ensure_ascii=False)}\n"
        )
    return text + '\nОтветь только JSON: {"html": "<разметка>", "css": "<стили>", "js": "<тело функции>"}\n'


def clean_code(data: object) -> dict:
    """html/css/js аниматора: строки в пределах длины, без запрещённого (AgentError с причиной)."""
    if not isinstance(data, dict):
        raise AgentError("ответ аниматора — не объект с html, css, js")
    out = {}
    for key, limit in CODE_LIMITS.items():
        value = data.get(key)
        if not isinstance(value, str):
            raise AgentError(f"нет куска «{key}»")
        if len(value) > limit:
            raise AgentError(f"«{key}» длиннее {limit} знаков")
        out[key] = value
    if not out["html"].strip() or not out["js"].strip():
        raise AgentError("пустая разметка или пустой таймлайн")
    if _HANDLER.search(out["html"]):
        raise AgentError("в «html» запрещено: обработчик в разметке")
    for key, value in out.items():
        value = _SVG_NS.sub("", value)
        for pattern, what in FORBIDDEN:
            if key == "css" and what in ("сеть", "таймер", "eval", "import()"):
                continue
            if pattern.search(value):
                raise AgentError(f"в «{key}» запрещено: {what}")
    if "</style" in out["css"].lower():
        raise AgentError("в css — закрывающий тег")
    return out


def css_vars(design: dict) -> str:
    pal = "".join(f"--{k}:{v};" for k, v in design["palette"].items())
    return f":root{{{pal}--font:{FONTS[design['font']]};--mono:{MONO[design['mono']]};}}"


def composition(board: dict, code: dict, design: dict, size: tuple[int, int]) -> str:
    """Документ HyperFrames: CSP без сети (кроме GSAP), корень #stage настоящего размера, внутри — логический
    кадр 1920 по ширине, масштабированный под рендер; таймлайн — window.__timelines.scene.

    ?preview в адресе — играет по кругу (превью во вкладке); в рендере таймлайн листает HyperFrames.
    """
    w, h = size
    lw, lh = LOGICAL_W, round(LOGICAL_W * h / w)
    x, y, zw, zh = zone(board["layout"], lw, lh)
    d = round(board["end"] - board["at"], 3)
    beats = [{"t": b["t"], "action": b["action"], "text": b["text"]} for b in board["beats"]]
    csp = (
        "default-src 'none'; script-src 'unsafe-inline' https://cdn.jsdelivr.net; style-src 'unsafe-inline'; "
        "img-src data: blob:; font-src data:"
    )
    title = html.escape(board["title"])
    beats_js = json.dumps(beats, ensure_ascii=False).replace("<", "\\u003c")
    full = "background:var(--bg);" if board["layout"] == "full" else ""
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{csp}">
<title>{title}</title>
<script src="{GSAP}"></script>
<style>
{css_vars(design)}
html,body{{margin:0;padding:0;width:{w}px;height:{h}px;background:transparent;overflow:hidden}}
#stage{{position:relative;width:{w}px;height:{h}px;overflow:hidden}}
#frame{{position:absolute;left:0;top:0;width:{lw}px;height:{lh}px;transform:scale({w / lw:.6f});transform-origin:0 0;
font-family:var(--font);color:var(--ink)}}
#scene{{position:absolute;left:{x}px;top:{y}px;width:{zw}px;height:{zh}px;overflow:hidden;{full}}}
#scene *{{box-sizing:border-box}}
</style>
<style>
{code["css"]}
</style>
</head><body>
<div id="stage" data-composition-id="scene" data-start="0" data-duration="{d}" data-width="{w}" data-height="{h}">
<div id="frame" class="clip" data-start="0" data-duration="{d}" data-track-index="0">
<div id="scene" class="layout-{board["layout"]}">
{code["html"]}
</div>
</div>
</div>
<script>
const tl = gsap.timeline({{ paused: true }});
(function (tl, D, ZW, ZH, BEATS) {{
{code["js"]}
}})(tl, {d}, {zw}, {zh}, {beats_js});
tl.set({{}}, {{}}, {d});
window.__timelines = window.__timelines || {{}};
window.__timelines.scene = tl;
if (new URLSearchParams(location.search).has("preview")) {{ tl.repeat(-1).repeatDelay(0.6).play(0); }}
</script>
</body></html>
"""


def js_check_source(code: dict) -> str:
    """Скрипт для `node --check`: тело аниматора в той же обёртке, что в композиции."""
    return f"const tl = null;\n(function (tl, D, ZW, ZH, BEATS) {{\n{code['js']}\n}});\n"
