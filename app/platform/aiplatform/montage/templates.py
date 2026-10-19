"""Каталог моушн-шаблонов (MASTER_SPEC §5.5): 12 детерминированных композиций HyperFrames + stat и quote.

Шаблон — типизированные props (FIELDS: тип, лимиты), разметка `html(props)`, таймлайн GSAP
`anim(props, d)`, ограничения (минимальный кегль, число элементов). Всё детерминировано: ни
Math.random, ни времени вне таймлайна; цвета и шрифт — CSS-переменные стиль-библии (style.css_vars),
без них — значения по умолчанию из assets/motion/style.css. JSON-схемы props —
contracts/montage/templates/<id>.schema.json, их собирает `schema_of` (тест следит, что файлы
совпадают с кодом). Старые вставки «Монтажа» (title, lower_third, stat, list, quote) — те же шаблоны
через ALIASES.
"""

from __future__ import annotations

import html
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from aiplatform.montage import schemas

TEMPLATE_VERSION = 1  # поднять, если меняется разметка или анимация: сцены перерисуются (хэш)
ASSETS = Path(__file__).resolve().parent / "assets"
STYLE_FILE = ASSETS / "motion" / "style.css"
SCHEMA_DIR = schemas.DIR / "templates"
GSAP = "https://cdn.jsdelivr.net/npm/gsap@3.12.5/dist/gsap.min.js"
W, H = 1920, 1080
MIN_S, MAX_S = 2.5, 12.0
MIN_FONT = 28
ALIASES = {"title": "chapter_title", "list": "step_list"}


class TemplateError(ValueError):
    """Props не годятся: нет главного текста, мало элементов, неизвестный шаблон."""


@dataclass(frozen=True)
class Field:
    kind: str  # str | num | int | enum | list | objlist
    limit: int = 0  # str: знаков; list/objlist: элементов
    min_items: int = 0
    item: int | dict | None = None  # list: знаков в элементе; objlist: {поле: знаков}; enum: нет
    choices: tuple[str, ...] = ()
    lo: float = 0.0
    hi: float = 0.0
    required: bool = False


@dataclass(frozen=True)
class Template:
    id: str
    label: str
    visual_type: str
    fields: dict[str, Field]
    default_s: float
    min_font: int
    hint: str  # для промпта раскадровщика: что показывать этим шаблоном
    example: dict


def _s(limit: int, required: bool = False) -> Field:
    return Field("str", limit, required=required)


def _lst(limit: int, item: int, min_items: int, required: bool = True) -> Field:
    return Field("list", limit, min_items, item, required=required)


def _objs(limit: int, item: dict, min_items: int) -> Field:
    return Field("objlist", limit, min_items, item, required=True)


CATALOG: dict[str, Template] = {}


def _add(t: Template) -> None:
    CATALOG[t.id] = t


_add(Template("kinetic_typography", "Кинетическая типографика", "kinetic",
              {"text": _s(90, True), "accent": _lst(4, 20, 0, required=False)}, 4.0, 56,
              "ключевая мысль словами, которые появляются по одному; accent — слова, которые выделить",
              {"text": "Канал — это очередь с правилами", "accent": ["очередь"]}))  # fmt: skip
_add(Template("lower_third", "Подпись-название", "callout", {"title": _s(40, True), "subtitle": _s(60)}, 4.0, 36,
              "название продукта, человека, репозитория и что это", {"title": "HyperFrames", "subtitle": "рендер HTML в видео"}))  # fmt: skip
_add(Template("chapter_title", "Заголовок главы", "title", {"title": _s(40, True), "kicker": _s(30)}, 3.5, 30,
              "начало новой части ролика", {"title": "Планировщик горутин", "kicker": "Часть 2"}))  # fmt: skip
_add(Template("concept_cards", "Карточки понятий", "cards",
              {"title": _s(40), "cards": _objs(4, {"title": 24, "text": 60}, 2)}, 6.0, 30,
              "2–4 понятия рядом: термин и что он значит",
              {"title": "Три кита", "cards": [{"title": "Горутина", "text": "лёгкий поток"},
                                             {"title": "Канал", "text": "очередь между горутинами"}]}))  # fmt: skip
_add(Template("flow_diagram", "Схема-поток", "flow",
              {"title": _s(40), "nodes": _lst(5, 18, 2), "direction": Field("enum", choices=("right", "down"))}, 6.0, 32,
              "процесс из 2–5 шагов со стрелками: запрос идёт по слоям, данные по стадиям",
              {"title": "Путь запроса", "nodes": ["HTTP", "TCP", "IP", "Ethernet"], "direction": "right"}))  # fmt: skip
_add(Template("comparison", "Сравнение", "comparison",
              {"left_title": _s(20, True), "right_title": _s(20, True), "left": _lst(4, 30, 1), "right": _lst(4, 30, 1)},
              6.0, 32, "две колонки: было/стало, А против Б",
              {"left_title": "Mutex", "right_title": "Channel", "left": ["общая память", "блокировка"],
               "right": ["передача владения", "очередь"]}))  # fmt: skip
_add(Template("timeline", "Хронология", "timeline",
              {"title": _s(40), "events": _objs(6, {"label": 12, "text": 40}, 2)}, 6.0, 30,
              "2–6 событий по порядку: версии, годы, этапы",
              {"title": "Go", "events": [{"label": "2009", "text": "анонс"}, {"label": "2012", "text": "Go 1.0"},
                                         {"label": "2022", "text": "дженерики"}]}))  # fmt: skip
_add(Template("step_list", "Список шагов", "list", {"title": _s(40), "steps": _lst(5, 40, 2)}, 5.0, 36,
              "2–5 пунктов или шагов, которые автор перечисляет",
              {"title": "Что сделать", "steps": ["Собрать", "Проверить", "Выложить"]}))  # fmt: skip
_add(Template("code_highlight", "Код с подсветкой", "code",
              {"language": _s(12), "code": _s(800, True), "highlight": Field("intlist", 6, 0), "caption": _s(60)}, 6.0, 30,
              "короткий фрагмент кода (до 12 строк по 64 знака), highlight — номера строк по порядку",
              {"language": "go", "code": "ch := make(chan int, 2)\nch <- 1\nch <- 2\nch <- 3 // блок", "highlight": [1, 4],
               "caption": "буфер на два значения"}))  # fmt: skip
_add(Template("task_scheduler", "Планировщик задач", "diagram",
              {"title": _s(40), "workers": _lst(4, 12, 1), "tasks": _lst(6, 12, 2)}, 6.0, 30,
              "очередь задач раскладывается по исполнителям: горутины по P, запросы по воркерам",
              {"title": "Планировщик", "workers": ["P0", "P1"], "tasks": ["G1", "G2", "G3", "G4"]}))  # fmt: skip
_add(Template("data_chart", "График", "chart",
              {"title": _s(40), "unit": _s(10), "kind": Field("enum", choices=("bar", "line")),
               "series": _objs(8, {"label": 12, "value": "num"}, 2)}, 6.0, 30,
              "2–8 чисел, которые автор сравнивает: столбцы или линия",
              {"title": "Задержка", "unit": "мс", "kind": "bar",
               "series": [{"label": "v1", "value": 120}, {"label": "v2", "value": 45}, {"label": "v3", "value": 20}]}))  # fmt: skip
_add(Template("callout_zoom", "Выноска", "callout",
              {"text": _s(60, True), "x_pct": Field("num", lo=0, hi=100), "y_pct": Field("num", lo=0, hi=100),
               "radius_pct": Field("num", lo=5, hi=40)}, 4.0, 36,
              "кольцо-выноска в точке кадра с подписью: куда смотреть",
              {"text": "вот здесь буфер полон", "x_pct": 70, "y_pct": 40, "radius_pct": 12}))  # fmt: skip
_add(Template("stat", "Цифра", "callout", {"value": _s(8, True), "label": _s(60)}, 4.0, 42,
              "одна цифра, которую важно запомнить", {"value": "×9", "label": "быстрее прошлой версии"}))  # fmt: skip
_add(Template("quote", "Цитата", "kinetic", {"text": _s(140, True), "author": _s(40)}, 5.0, 34,
              "мысль-вывод, которую хочется выделить (редко)", {"text": "Не общайтесь через память — делитесь памятью через общение", "author": "Go proverbs"}))  # fmt: skip

CORE = ("kinetic_typography", "lower_third", "chapter_title", "concept_cards", "flow_diagram", "comparison",
        "timeline", "step_list", "code_highlight", "task_scheduler", "data_chart", "callout_zoom")  # fmt: skip


def resolve(tid: object) -> str:
    key = ALIASES.get(str(tid), str(tid))
    if key not in CATALOG:
        raise TemplateError(f"неизвестный шаблон {tid!r}")
    return key


def get(tid: object) -> Template:
    return CATALOG[resolve(tid)]


# ---------- props: чистка и схема ----------


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _code(value: object, limit: int, lines: int = 12, width: int = 64) -> str:
    rows = [ln.rstrip()[:width] for ln in str(value or "").replace("\t", "    ").splitlines()]
    return "\n".join(rows[:lines])[:limit].strip("\n")


def clean_props(tid: object, props: object) -> dict:
    """Props по FIELDS: строки обрезаны, списки в пределах, числа в границах; нехватка → TemplateError."""
    t = get(tid)
    src = props if isinstance(props, dict) else {}
    out: dict = {}
    for name, f in t.fields.items():
        value = src.get(name)
        if f.kind == "str":
            out[name] = _code(value, f.limit) if name == "code" else _text(value, f.limit)
            if f.required and not out[name]:
                raise TemplateError(f"{t.id}: пустое поле «{name}»")
        elif f.kind == "enum":
            out[name] = value if value in f.choices else f.choices[0]
        elif f.kind == "num":
            try:
                out[name] = round(min(f.hi, max(f.lo, float(value))), 2)
            except (TypeError, ValueError):
                out[name] = round((f.lo + f.hi) / 2, 2)
        elif f.kind == "intlist":
            raw = value if isinstance(value, list) else []
            ints = sorted({int(x) for x in raw if isinstance(x, int | float) and not isinstance(x, bool) and x >= 1})
            out[name] = ints[: f.limit]
        elif f.kind == "list":
            raw = value if isinstance(value, list) else []
            items = [_text(x, f.item) for x in raw if isinstance(x, str | int | float)]
            out[name] = [x for x in items if x][: f.limit]
            if len(out[name]) < f.min_items:
                raise TemplateError(f"{t.id}: в «{name}» меньше {f.min_items} элементов")
        elif f.kind == "objlist":
            raw = value if isinstance(value, list) else []
            items = []
            for obj in raw:
                if not isinstance(obj, dict):
                    continue
                item = {}
                for key, lim in f.item.items():  # type: ignore[union-attr]
                    if lim == "num":
                        try:
                            item[key] = round(float(obj.get(key)), 3)
                        except (TypeError, ValueError):
                            item[key] = 0.0
                    else:
                        item[key] = _text(obj.get(key), lim)
                if any(isinstance(v, str) and v for v in item.values()):
                    items.append(item)
            out[name] = items[: f.limit]
            if len(out[name]) < f.min_items:
                raise TemplateError(f"{t.id}: в «{name}» меньше {f.min_items} элементов")
    if t.id == "code_highlight":
        n = len(out["code"].splitlines())
        out["highlight"] = [i for i in out["highlight"] if i <= n]
    return out


def schema_of(tid: str) -> dict:
    """JSON Schema props шаблона из FIELDS (файл contracts/montage/templates/<id>.schema.json)."""
    t = CATALOG[tid]
    props: dict = {}
    required = []
    for name, f in t.fields.items():
        if f.kind == "str":
            props[name] = {"type": "string", "maxLength": f.limit}
        elif f.kind == "enum":
            props[name] = {"type": "string", "enum": list(f.choices)}
        elif f.kind == "num":
            props[name] = {"type": "number", "minimum": f.lo, "maximum": f.hi}
        elif f.kind == "intlist":
            props[name] = {"type": "array", "items": {"type": "integer", "minimum": 1}, "maxItems": f.limit}
        elif f.kind == "list":
            props[name] = {"type": "array", "items": {"type": "string", "maxLength": f.item}, "minItems": f.min_items,
                           "maxItems": f.limit}  # fmt: skip
        elif f.kind == "objlist":
            fields = {
                k: ({"type": "number"} if v == "num" else {"type": "string", "maxLength": v}) for k, v in f.item.items()
            }  # type: ignore[union-attr]
            props[name] = {"type": "array", "minItems": f.min_items, "maxItems": f.limit,
                           "items": {"type": "object", "required": list(fields), "properties": fields}}  # fmt: skip
        if f.required:
            required.append(name)
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"montage/templates/{tid}",
        "title": tid,
        "description": f"{t.label}: {t.hint}. Длительность по умолчанию {t.default_s} с, минимальный кегль {t.min_font}px.",
        "type": "object",
        "required": required,
        "properties": props,
        "examples": [t.example],
    }


def write_schemas(folder: Path = SCHEMA_DIR) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    out = []
    for tid in CATALOG:
        path = folder / f"{tid}.schema.json"
        path.write_text(json.dumps(schema_of(tid), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        out.append(path)
    return out


def hints() -> str:
    """Каталог для промпта раскадровщика: id — зачем — пример props."""
    lines = []
    for tid in (*CORE, "stat", "quote"):
        t = CATALOG[tid]
        lines.append(f"- {tid} ({t.label}): {t.hint}. Пример props: {json.dumps(t.example, ensure_ascii=False)}")
    return "\n".join(lines)


def public() -> list[dict]:
    return [{"id": t.id, "label": t.label, "visual_type": t.visual_type, "hint": t.hint, "default_s": t.default_s,
             "example": t.example, "core": t.id in CORE} for t in CATALOG.values()]  # fmt: skip


# ---------- разметка и анимация ----------


def _e(text: str) -> str:
    return html.escape(text, quote=True)


def _enter(sel: str, style: dict | None, extra: str = "", at: float = 0.1) -> str:
    ease = (style or {}).get("motion", {}).get("enter", "power3.out")
    d = (style or {}).get("motion", {}).get("enter_s", 0.5)
    return f'tl.from("{sel}", {{y: 50, opacity: 0, duration: {d}, ease: "{ease}"{extra}}}, {at});'


def _stagger(sel: str, style: dict | None, each: float, at: float, props: str = "x: -40, opacity: 0") -> str:
    ease = (style or {}).get("motion", {}).get("enter", "power3.out")
    return f'tl.from("{sel}", {{{props}, duration: 0.4, stagger: {each}, ease: "{ease}"}}, {at});'


def render(tid: object, props: dict, style: dict | None = None, d: float = 4.0) -> tuple[str, str]:
    """(разметка карточки, таймлайн GSAP без выхода) — props уже после clean_props."""
    key = resolve(tid)
    f = props
    if key == "kinetic_typography":
        accent = {a.lower() for a in f.get("accent", [])}
        words = "".join(
            f'<span class="kt-word{" kt-accent" if w.strip(".,!?:;«»").lower() in accent else ""}">{_e(w)}</span> '
            for w in f["text"].split()
        )
        card = f'<div class="card pos-center kinetic">{words}</div>'
        anim = (_stagger(".kt-word", style, 0.12, 0.1, "y: 60, opacity: 0, rotateX: 40")
                + 'tl.to(".kt-accent", {scale: 1.12, color: "var(--accent2, #ffb454)", duration: 0.3, ease: "back.out(2)"}, "-=0.1");')  # fmt: skip
    elif key == "lower_third":
        sub = f'<div class="sub">{_e(f["subtitle"])}</div>' if f.get("subtitle") else ""
        card = f'<div class="card pos-lower"><div class="bar"></div><div class="headline">{_e(f["title"])}</div>{sub}</div>'
        anim = (_enter(".card", style, ", x: -80, y: 0")
                + 'tl.from(".bar", {scaleY: 0, transformOrigin: "50% 100%", duration: 0.4, ease: "power2.out"}, 0.3);')  # fmt: skip
    elif key == "chapter_title":
        kicker = f'<div class="kicker">{_e(f["kicker"])}</div>' if f.get("kicker") else ""
        card = f'<div class="card pos-center">{kicker}<div class="headline">{_e(f["title"])}</div></div>'
        anim = 'tl.from(".card", {scale: 0.92, opacity: 0, duration: 0.6, ease: "back.out(1.6)"}, 0.1);'
    elif key == "concept_cards":
        cards = "".join(f'<div class="concept"><div class="c-title">{_e(c["title"])}</div><div class="c-text">{_e(c["text"])}</div></div>'
                        for c in f["cards"])  # fmt: skip
        title = f'<div class="kicker">{_e(f["title"])}</div>' if f.get("title") else ""
        card = f'<div class="card pos-wide">{title}<div class="concepts">{cards}</div></div>'
        anim = _enter(".card", style) + _stagger(".concept", style, 0.3, 0.4, "y: 40, opacity: 0, scale: 0.9")
    elif key == "flow_diagram":
        nodes = f["nodes"]
        parts = []
        for n, node in enumerate(nodes):
            if n:
                parts.append('<div class="arrow"><span></span></div>')
            parts.append(f'<div class="node">{_e(node)}</div>')
        title = f'<div class="kicker">{_e(f["title"])}</div>' if f.get("title") else ""
        card = f'<div class="card pos-wide"><div class="flow flow-{f["direction"]}">{"".join(parts)}</div>{title}</div>'
        anim = (_enter(".card", style) + _stagger(".node", style, 0.45, 0.4, "scale: 0.6, opacity: 0")
                + 'tl.from(".arrow span", {scaleX: 0, transformOrigin: "0 50%", duration: 0.3, stagger: 0.45, ease: "power2.out"}, 0.65);')  # fmt: skip
    elif key == "comparison":

        def col(title: str, items: list[str], cls: str) -> str:
            lis = "".join(f"<li>{_e(x)}</li>" for x in items)
            return f'<div class="col {cls}"><div class="c-title">{_e(title)}</div><ul>{lis}</ul></div>'

        card = f'<div class="card pos-wide"><div class="compare">{col(f["left_title"], f["left"], "left")}{col(f["right_title"], f["right"], "right")}</div></div>'
        anim = _enter(".card", style) + _stagger(".col li", style, 0.25, 0.5)
    elif key == "timeline":
        events = "".join(f'<div class="event"><div class="dot"></div><div class="e-label">{_e(ev["label"])}</div><div class="e-text">{_e(ev["text"])}</div></div>'
                         for ev in f["events"])  # fmt: skip
        title = f'<div class="kicker">{_e(f["title"])}</div>' if f.get("title") else ""
        card = f'<div class="card pos-wide">{title}<div class="tl-line"></div><div class="events">{events}</div></div>'
        anim = (_enter(".card", style)
                + 'tl.from(".tl-line", {scaleX: 0, transformOrigin: "0 50%", duration: 1.0, ease: "power2.inOut"}, 0.4);'
                + _stagger(".event", style, 0.3, 0.5, "y: 30, opacity: 0"))  # fmt: skip
    elif key == "step_list":
        items = "".join(f"<li><b>{n:02d}</b><span>{_e(t)}</span></li>" for n, t in enumerate(f["steps"], 1))
        kicker = f'<div class="kicker">{_e(f["title"])}</div>' if f.get("title") else ""
        card = f'<div class="card pos-left">{kicker}<ol class="items">{items}</ol></div>'
        anim = _enter(".card", style) + _stagger(".items li", style, 0.35, 0.4)
    elif key == "code_highlight":
        rows = f["code"].splitlines()
        hl = set(f.get("highlight", []))
        lines = "".join(f'<div class="ln{" hl" if n in hl else ""}"><b>{n}</b><code>{_e(ln) or " "}</code></div>'
                        for n, ln in enumerate(rows, 1))  # fmt: skip
        lang = f'<div class="kicker">{_e(f["language"])}</div>' if f.get("language") else ""
        caption = f'<div class="sub">{_e(f["caption"])}</div>' if f.get("caption") else ""
        card = f'<div class="card pos-left code">{lang}<pre class="code-box">{lines}</pre>{caption}</div>'
        anim = (_enter(".card", style) + _stagger(".ln", style, 0.08, 0.3, "x: -20, opacity: 0")
                + 'tl.to(".ln.hl", {backgroundColor: "rgba(255,180,84,0.28)", duration: 0.35, stagger: 0.6}, 1.2);')  # fmt: skip
    elif key == "task_scheduler":
        lanes = "".join(f'<div class="lane"><div class="lane-name">{_e(w)}</div><div class="lane-slots" data-lane="{n}"></div></div>'
                        for n, w in enumerate(f["workers"]))  # fmt: skip
        tasks = "".join(f'<div class="task" data-task="{n}">{_e(t)}</div>' for n, t in enumerate(f["tasks"]))
        title = f'<div class="kicker">{_e(f["title"])}</div>' if f.get("title") else ""
        card = f'<div class="card pos-wide">{title}<div class="sched"><div class="queue">{tasks}</div><div class="lanes">{lanes}</div></div></div>'
        n_workers = max(1, len(f["workers"]))
        moves = "".join(
            f'tl.to("[data-task=\\"{n}\\"]", {{x: 460 + {n // n_workers} * 150, y: {(n % n_workers) * 118 - n * 70}, duration: 0.5, ease: "power2.inOut"}}, {0.8 + n * 0.45:.2f});'
            for n in range(len(f["tasks"]))
        )
        anim = _enter(".card", style) + _stagger(".task", style, 0.1, 0.3, "x: -30, opacity: 0") + moves
    elif key == "data_chart":
        card, anim = _chart(f, style)
    elif key == "callout_zoom":
        r = f["radius_pct"] * H / 100
        cx, cy = f["x_pct"] * W / 100, f["y_pct"] * H / 100
        label_left = cx + r + 40 if cx < W * 0.6 else max(80.0, cx - r - 40 - 700)
        card = (f'<div class="callout" style="left:{cx - r:.0f}px;top:{cy - r:.0f}px;width:{2 * r:.0f}px;height:{2 * r:.0f}px"></div>'
                f'<div class="card pos-abs" style="left:{label_left:.0f}px;top:{max(60.0, cy - 60):.0f}px"><div class="headline small">{_e(f["text"])}</div></div>')  # fmt: skip
        anim = ('tl.from(".callout", {scale: 2.2, opacity: 0, duration: 0.6, ease: "power3.out"}, 0.1);'
                + _enter(".card", style, at=0.4))  # fmt: skip
    elif key == "stat":
        card = f'<div class="card pos-left"><div class="value">{_e(f["value"])}</div><div class="label">{_e(f["label"])}</div></div>'
        anim = (
            _enter(".card", style)
            + 'tl.from(".value", {scale: 0.6, opacity: 0, duration: 0.7, ease: "back.out(2)"}, 0.3);'
        )
    else:  # quote
        author = f'<div class="author">— {_e(f["author"])}</div>' if f.get("author") else ""
        card = f'<div class="card pos-center"><div class="quote">«{_e(f["text"])}»</div>{author}</div>'
        anim = _enter(".card", style, ", y: 40")
    return card, anim


def _chart(f: dict, style: dict | None) -> tuple[str, str]:
    series = f["series"]
    values = [max(0.0, s["value"]) for s in series]
    top = max(values) or 1.0
    width, height, pad = 1000, 420, 60
    step = (width - 2 * pad) / max(1, len(series))
    parts = []
    points = []
    for n, (s, v) in enumerate(zip(series, values, strict=True)):
        x = pad + n * step
        h_px = (height - 2 * pad) * v / top
        y = height - pad - h_px
        points.append((x + step / 2, y))
        if f["kind"] == "bar":
            parts.append(
                f'<rect class="bar-rect" x="{x + step * 0.15:.0f}" y="{y:.0f}" width="{step * 0.7:.0f}" height="{h_px:.0f}" rx="10"/>'
            )
        parts.append(
            f'<text class="val" x="{x + step / 2:.0f}" y="{y - 14:.0f}" text-anchor="middle">{_e(f"{v:g}")}{_e(f.get("unit", ""))}</text>'
        )
        parts.append(
            f'<text class="lbl" x="{x + step / 2:.0f}" y="{height - pad + 40:.0f}" text-anchor="middle">{_e(s["label"])}</text>'
        )
    if f["kind"] == "line":
        path = " ".join(f"{x:.0f},{y:.0f}" for x, y in points)
        parts.insert(0, f'<polyline class="line" points="{path}"/>')
        parts += [f'<circle class="pt" cx="{x:.0f}" cy="{y:.0f}" r="10"/>' for x, y in points]
    title = f'<div class="kicker">{_e(f["title"])}</div>' if f.get("title") else ""
    card = (f'<div class="card pos-wide chart">{title}<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}">'
            f'<line class="axis" x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}"/>{"".join(parts)}</svg></div>')  # fmt: skip
    grow = ('tl.from(".bar-rect", {scaleY: 0, transformOrigin: "50% 100%", duration: 0.6, stagger: 0.15, ease: "power3.out"}, 0.4);'
            if f["kind"] == "bar" else
            'tl.from(".line", {strokeDashoffset: 3000, duration: 1.2, ease: "power2.inOut"}, 0.4);'
            'tl.from(".pt", {scale: 0, transformOrigin: "50% 50%", duration: 0.3, stagger: 0.15}, 0.8);')  # fmt: skip
    anim = _enter(".card", style) + grow + _stagger(".val, .lbl", style, 0.08, 0.9, "opacity: 0, y: 10")
    return card, anim


# ---------- документ HyperFrames ----------


def stylesheet() -> str:
    return STYLE_FILE.read_text(encoding="utf-8")


def document(card: str, anim: str, *, duration: float, css: str, comp_id: str = "insert",
             style: dict | None = None, csp: bool = False, title: str = "") -> str:  # fmt: skip
    """HTML-композиция: корень #stage 1920×1080, один клип, таймлайн GSAP в window.__timelines[comp_id].

    Выход — общий для всех шаблонов: карточка уходит вверх за exit_s до конца. ?preview в адресе —
    играет по кругу (превью во вкладке)."""
    exit_s = (style or {}).get("motion", {}).get("exit_s", 0.45)
    exit_ease = (style or {}).get("motion", {}).get("exit", "power2.in")
    center = 'gsap.set(".pos-center", {xPercent: -50, yPercent: -50});' if "pos-center" in card else ""
    head_csp = (
        "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; script-src 'unsafe-inline' "
        "https://cdn.jsdelivr.net; style-src 'unsafe-inline'; img-src data:\">\n"
        if csp
        else ""
    )
    d = round(duration, 3)
    return f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
{head_csp}<title>{_e(title or comp_id)}</title>
<script src="{GSAP}"></script>
<style>{css}</style>
</head><body>
<div id="stage" data-composition-id="{comp_id}" data-start="0" data-duration="{d}" data-width="{W}" data-height="{H}">
  <div class="clip" data-start="0" data-duration="{d}" data-track-index="0">{card}</div>
</div>
<script>
{center}
const tl = gsap.timeline({{ paused: true }});
{anim}
tl.to(".card, .callout", {{opacity: 0, y: -24, duration: {exit_s}, ease: "{exit_ease}"}}, {max(0.0, d - exit_s - 0.1):.2f});
tl.set({{}}, {{}}, {d});
window.__timelines = window.__timelines || {{}};
window.__timelines.{comp_id} = tl;
if (new URLSearchParams(location.search).has("preview")) {{ tl.repeat(-1).repeatDelay(0.6).play(0); }}
</script>
</body></html>
"""


def composition(tid: object, props: dict, style: dict | None, duration_s: float, *, title: str = "") -> str:
    """Документ сцены раскадровки: CSP без сети (кроме GSAP), переменные стиль-библии, таймлайн scene."""
    from aiplatform.montage import style as style_module

    card, anim = render(tid, props, style, duration_s)
    css = (style_module.css_vars(style) + "\n" if style else "") + stylesheet()
    return document(card, anim, duration=duration_s, css=css, comp_id="scene", style=style, csp=True, title=title)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args != ["--schemas"]:
        print("usage: python -m aiplatform.montage.templates --schemas", file=sys.stderr)
        return 2
    for path in write_schemas():
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
