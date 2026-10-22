"""Динамические слайды: фильтры ffmpeg, которые рисуют сцену по времени.

Виды — как сцены фильма narrator (tools/film): пункты, крупное число, график
столбцами или линией, схема из шагов, сравнение «было / стало», цитата.

Ритм: элементы сцены появляются равномерно по длине слайда, но не реже раза
в BEAT_S секунд. Всё показано — кадр стоит: движение только при появлении
(PRD-031), бегущая подсветка мешала читать и укачивала.

Как анимируем: у drawtext x, y и alpha считаются на каждом кадре — текст
выезжает и проявляется плавно. Canvas.animate=False (настройка slide_motion) —
без движения: ни выезда, ни роста, ни счёта, элементы только проявляются. Выражения drawbox ffmpeg считает один раз,
поэтому рост столбцов, полос и линия графика — ступенями: каждая ступень —
свой drawbox с enable по времени.
"""

from __future__ import annotations

import math
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

BEAT_S = 4.0  # пока не всё показано, новый элемент — не реже раза в BEAT_S секунд
START_S = 0.5  # первый элемент — сразу за заголовком
TAIL_S = 1.0  # последний элемент — не позже чем за секунду до конца слайда
GROW_S = 0.6  # рост столбца или полосы
STEPS = 12  # ступеней роста
ACCENT = "0x7ea2ff"
GOOD, BAD = "0x5ee6a0", "0xff7a7a"
INK, MUTED = "white", "0xb4bccc"
PANEL = "white@0.07"
DARK = "0x141a26"


def wrap(text: str, chars: int, lines: int) -> str:
    out = textwrap.wrap(" ".join(text.split()), width=chars) or [""]
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1].rstrip(" .,;:") + "…"
    return "\n".join(out)


def quote(path: Path | str) -> str:
    """Путь в аргументе фильтра ffmpeg: в одинарных кавычках, кавычки и \\ экранированы."""
    return "'" + str(path).replace("\\", "\\\\").replace("'", r"'\''") + "'"


# --- расписание ---


def reveal_times(n: int, seconds: float, start: float = START_S, tail: float = TAIL_S) -> list[float]:
    """Когда появляется каждый из n элементов: равномерно по слайду, шаг не больше BEAT_S."""
    if n <= 0:
        return []
    if n == 1:
        return [start]
    gap = min(BEAT_S, max(seconds - start - tail, 0.0) / (n - 1))
    return [round(start + i * gap, 3) for i in range(n)]


def windows(t0: float, seconds: float, steps: int) -> list[tuple[float, float | None]]:
    """Окна ступеней: k-я видна с t0 + seconds·k/steps до следующей, последняя — до конца слайда."""
    edges = [t0 + seconds * k / steps for k in range(steps)]
    return [(edges[k], edges[k + 1] if k + 1 < steps else None) for k in range(steps)]


def window(a: float, b: float | None) -> str:
    return f"gte(t,{a:.3f})" if b is None else f"gte(t,{a:.3f})*lt(t,{b:.3f})"


def ease(p: float) -> float:
    p = min(max(p, 0.0), 1.0)
    return p * p * (3 - 2 * p)


def _p(t0: float, d: float) -> str:
    return f"min(1,max(0,(t-{t0:.3f})/{d:.3f}))"


def appear(t0: float, d: float = 0.35) -> str:
    return _p(t0, d)


def slide_x(x: int, t0: float, dx: int, d: float = 0.45) -> str:
    return f"{x}-{dx}*pow(1-{_p(t0, d)},3)"


def slide_y(y: int, t0: float, dy: int, d: float = 0.45) -> str:
    return f"{y}+{dy}*pow(1-{_p(t0, d)},3)"


# --- числа ---


def decimals(value: float) -> int:
    text = f"{abs(value):.2f}".rstrip("0").rstrip(".")
    return len(text.partition(".")[2])


def number(value: float, digits: int) -> str:
    """Число по-русски: пробел между тысячами, запятая перед дробной частью."""
    text = f"{abs(value):,.{digits}f}".replace(",", " ").replace(".", ",")
    return ("−" if value < 0 and round(abs(value), digits) else "") + text


def line_h(size: int) -> int:
    """Шаг строки drawtext: высота строки шрифта (~1,15 кегля) плюс line_spacing (size // 4)."""
    return round(size * 1.4)


def text_h(text: str, size: int) -> int:
    """Высота блока drawtext из нескольких строк: между строками — line_spacing, после последней — нет."""
    return round(size * (1.15 * lines_of(text) + 0.25 * (lines_of(text) - 1)))


def lines_of(text: str) -> int:
    return text.count("\n") + 1


# --- холст ---


@dataclass
class Canvas:
    """Куда рисовать: размер кадра, поля, верх и низ свободного места и шрифт.
    parts — фильтры по порядку, тексты — файлы в work (expansion=none)."""

    w: int
    h: int
    margin: int
    font: str
    work: Path
    top: int
    bottom: int
    animate: bool = True  # False — без движения: элементы только проявляются
    parts: list[str] = field(default_factory=list)
    files: int = 0

    @property
    def wide(self) -> bool:
        return self.w > self.h * 1.2

    def px(self, value: float) -> int:
        return max(1, round(value * min(self.w, self.h) / 1080))

    @staticmethod
    def chars(width: float, size: int) -> int:
        return max(int(width / (size * 0.55)), 6)

    def slide_x(self, x: int, t0: float, dx: int) -> int | str:
        return slide_x(x, t0, dx) if self.animate else x

    def slide_y(self, y: int, t0: float, dy: int) -> int | str:
        return slide_y(y, t0, dy) if self.animate else y

    def file(self, text: str) -> Path:
        self.files += 1
        path = self.work / f"m{self.files:03d}.txt"
        path.write_text(text, encoding="utf-8")
        return path

    def text(
        self,
        text: str,
        size: int,
        x: int | str,
        y: int | str,
        *,
        color: str = INK,
        alpha: str = "1",
        enable: str | None = None,
    ) -> None:
        font = f"fontfile={quote(self.font)}:" if self.font else ""
        part = (
            f"drawtext={font}textfile={quote(self.file(text))}:expansion=none:fontsize={size}"
            f":fontcolor={color}:line_spacing={size // 4}:x='{x}':y='{y}':alpha='{alpha}'"
        )
        self.parts.append(part + (f":enable='{enable}'" if enable else ""))

    def box(
        self, x: int, y: int, w: int, h: int, color: str, *, enable: str | None = None, thick: str = "fill"
    ) -> None:
        part = f"drawbox=x={x}:y={y}:w={max(w, 1)}:h={max(h, 1)}:color={color}:t={thick}"
        self.parts.append(part + (f":enable='{enable}'" if enable else ""))

    def grow_right(self, x: int, y: int, w: int, h: int, color: str, t0: float, d: float, steps: int = STEPS) -> None:
        if not self.animate:
            self.box(x, y, w, h, color, enable=f"gte(t,{t0:.3f})")
            return
        for k, (a, b) in enumerate(windows(t0, d, steps), start=1):
            self.box(x, y, round(w * ease(k / steps)), h, color, enable=window(a, b))

    def grow_up(self, x: int, base: int, w: int, h: int, color: str, t0: float, d: float, steps: int = STEPS) -> None:
        if not self.animate:
            self.box(x, base - h, w, h, color, enable=f"gte(t,{t0:.3f})")
            return
        for k, (a, b) in enumerate(windows(t0, d, steps), start=1):
            grown = max(round(h * ease(k / steps)), 1)
            self.box(x, base - grown, w, grown, color, enable=window(a, b))

    def words(self, text: str, size: int, x: int | str, y: int, start: float, span: float, color: str = INK) -> None:
        """Уже разбитый на строки текст проявляется по словам: строки не прыгают — разбиты заранее."""
        rows = [row.split() for row in text.split("\n")]
        total = sum(len(r) for r in rows)
        groups = min(total, 14)
        if groups <= 1 or not self.animate:
            self.text(text, size, x, y, color=color, alpha=appear(start))
            return
        for g, (a, b) in enumerate(windows(start, span, groups), start=1):
            left, shown = math.ceil(total * g / groups), []
            for row in rows:
                take = row[:left]
                left -= len(take)
                if take:
                    shown.append(" ".join(take))
            self.text("\n".join(shown), size, x, y, color=color, enable=window(a, b))

    def heading(self, title: str, t0: float = 0.1) -> int:
        """Заголовок сверху: выезжает снизу, под ним растёт полоска акцента. Возвращает, где начать сцену."""
        size = self.px(62 if self.wide else 58)
        text = wrap(title, self.chars(self.w - 2 * self.margin, size), 2)
        self.text(text, size, self.margin, self.slide_y(self.top, t0, self.px(30)), alpha=appear(t0, 0.4))
        bar_y = self.top + text_h(text, size) + self.px(10)
        self.grow_right(self.margin, bar_y, self.px(160), self.px(8), ACCENT, t0 + 0.25, 0.4, steps=8)
        return bar_y + self.px(60)


# --- сцены ---


def title_only(c: Canvas, title: str, seconds: float) -> None:
    """Слайд без пунктов: крупный заголовок по центру, по словам."""
    size = c.px(92 if c.wide else 80)
    text = wrap(title, c.chars(c.w - 2 * c.margin, size), 3)
    y = (c.top + c.bottom) // 2 - text_h(text, size) // 2
    span = min(max(seconds * 0.5, 0.6), len(text.split()) * 0.35)
    c.words(text, size, c.margin, y, 0.15, span)
    bar_y = y + text_h(text, size) + c.px(14)
    c.grow_right(c.margin, bar_y, c.px(240), c.px(10), ACCENT, 0.15 + span, 0.5, steps=10)


def bullets(c: Canvas, scene: dict, seconds: float) -> None:
    items = scene.get("bullets") or []
    if not items:
        title_only(c, scene["title"], seconds)
        return
    y = c.heading(scene["title"])
    size, pad, gap, bar = c.px(46 if c.wide else 42), c.px(20), c.px(22), c.px(8)
    while True:
        width = c.w - 2 * c.margin - bar - 3 * pad
        texts = [wrap(item, c.chars(width, size), 2) for item in items]
        heights = [text_h(t, size) + 2 * pad for t in texts]
        if y + sum(heights) + gap * (len(items) - 1) <= c.bottom or size <= c.px(26):
            break
        size = round(size * 0.9)
    times = reveal_times(len(items), seconds)
    full = c.w - 2 * c.margin
    for text, height, t0 in zip(texts, heights, times, strict=True):
        on = f"gte(t,{t0:.3f})"
        tx = c.margin + bar + 2 * pad
        c.box(c.margin, y, full, height, PANEL, enable=on)
        c.box(c.margin, y, bar, height, ACCENT, enable=on)
        c.text(text, size, c.slide_x(tx, t0, c.px(60)), y + pad, alpha=appear(t0))
        y += height + gap


def stat(c: Canvas, scene: dict, seconds: float) -> None:
    top = c.heading(scene["title"]) if scene.get("title") else c.top
    value = float(scene["value"])
    digits = decimals(value)
    prefix, suffix = scene.get("prefix") or "", scene.get("suffix") or ""
    percent = suffix.strip() == "%" and 0 <= value <= 100
    size = c.px(200 if c.wide else 170)
    lsize = c.px(48 if c.wide else 44)
    label = wrap(scene.get("label") or "", c.chars(c.w - 2 * c.margin, lsize), 2) if scene.get("label") else ""
    bar_h = c.px(18)
    block = line_h(size) + (bar_h + c.px(50) if percent else 0) + (text_h(label, lsize) if label else 0)
    y = max(top, (top + c.bottom) // 2 - block // 2)
    t0, run = 0.4, min(2.5, max(0.8, seconds * 0.4))
    center = "(w-text_w)/2"
    if c.animate:
        _count(c, value, digits, (prefix, suffix), size, center, y, t0, run)
    else:  # без счёта: сразу итоговое число
        c.text(f"{prefix}{number(value, digits)}{suffix}", size, center, y, color=ACCENT, alpha=appear(0, 0.3))
    after = y + line_h(size)
    if percent:
        track = c.w - 4 * c.margin
        c.box(2 * c.margin, after, track, bar_h, "white@0.12", enable=f"gte(t,{t0:.3f})")
        c.grow_right(2 * c.margin, after, round(track * value / 100), bar_h, ACCENT, t0, run, steps=16)
        after += bar_h + c.px(50)
    if label:
        c.text(label, lsize, center, after, alpha=appear(t0 + run * 0.7, 0.5))


def _count(
    c: Canvas, value: float, digits: int, marks: tuple[str, str], size: int, center: str, y: int, t0: float, run: float
) -> None:
    """Число набирается от нуля до значения за run секунд."""
    prefix, suffix = marks
    c.text(
        f"{prefix}{number(0, digits)}{suffix}",
        size,
        center,
        y,
        color=ACCENT,
        alpha=appear(0, 0.3),
        enable=f"lt(t,{t0:.3f})",
    )
    steps = 20
    for k, (a, b) in enumerate(windows(t0, run, steps), start=1):
        shown = value * ease(k / steps)
        c.text(f"{prefix}{number(shown, digits)}{suffix}", size, center, y, color=ACCENT, enable=window(a, b))


def _chart_box(c: Canvas, top: int) -> tuple[int, int, int, int]:
    """Поле графика: x от и до, верх (над ним — подписи значений) и база (под ней — подписи точек)."""
    return c.margin, c.w - c.margin, top + c.px(70), c.bottom - c.px(60)


def _value(point: dict, unit: str) -> str:
    return number(point["value"], decimals(point["value"])) + (f" {unit}" if unit else "")


def bar_chart(c: Canvas, scene: dict, seconds: float) -> None:
    top = c.heading(scene["title"])
    data, unit = scene["data"], scene.get("unit") or ""
    x0, x1, y_top, base = _chart_box(c, top)
    n = len(data)
    slot = (x1 - x0) / n
    width = round(min(slot * 0.56, c.px(220)))
    peak = max(abs(p["value"]) for p in data) or 1
    vsize = c.px(40 if n <= 5 else 32)
    lsize = c.px(32 if n <= 5 else 26)
    c.box(x0, base, x1 - x0, c.px(3), "white@0.25", enable="gte(t,0.3)")
    times = reveal_times(n, seconds)
    for i, (point, t0) in enumerate(zip(data, times, strict=True)):
        bx = round(x0 + i * slot + (slot - width) / 2)
        height = max(round((base - y_top) * abs(point["value"]) / peak), c.px(4))
        color = ACCENT if abs(point["value"]) == peak else f"{ACCENT}@0.55"
        c.grow_up(bx, base, width, height, color, t0, GROW_S)
        mid = bx + width // 2
        c.text(
            _value(point, unit),
            vsize,
            f"{mid}-text_w/2",
            base - height - round(vsize * 1.4),
            alpha=appear(t0 + GROW_S * 0.6),
        )
        c.text(point["label"], lsize, f"{mid}-text_w/2", base + c.px(16), color=MUTED, alpha=appear(t0))


def _segment(c: Canvas, a: tuple[int, int], b: tuple[int, int], t0: float, d: float, dot: int) -> None:
    """Отрезок линии графика точками, которые появляются от a к b за d секунд."""
    length = math.dist(a, b)
    count = max(int(length / (dot * 0.6)), 2)
    for j in range(count + 1):
        x = round(a[0] + (b[0] - a[0]) * j / count) - dot // 2
        y = round(a[1] + (b[1] - a[1]) * j / count) - dot // 2
        c.box(x, y, dot, dot, ACCENT, enable=f"gte(t,{t0 + d * j / count:.3f})")


def line_chart(c: Canvas, scene: dict, seconds: float) -> None:
    top = c.heading(scene["title"])
    data, unit = scene["data"], scene.get("unit") or ""
    x0, x1, y_top, base = _chart_box(c, top)
    values = [p["value"] for p in data]
    lo, hi = min(min(values), 0.0), max(max(values), 0.0)
    if min(values) > 0 and hi - min(values) < hi * 0.5:  # все далеко от нуля — ось не с нуля
        lo = min(values) - (hi - min(values)) * 0.3
    span = (hi - lo) or 1.0
    n = len(data)
    pad = c.px(50)

    def at(i: int) -> tuple[int, int]:
        x = x0 + pad + (x1 - x0 - 2 * pad) * i / max(n - 1, 1)
        return round(x), round(base - (data[i]["value"] - lo) / span * (base - y_top))

    for share in (0.0, 0.5, 1.0):
        c.box(x0, round(y_top + (base - y_top) * share), x1 - x0, c.px(2), "white@0.12", enable="gte(t,0.3)")
    times = reveal_times(n, seconds)
    gap = times[1] - times[0] if n > 1 else 1.0
    draw = min(0.6, gap * 0.8)
    dot, mark = c.px(8), c.px(22)
    vsize, lsize = c.px(34 if n <= 6 else 28), c.px(30 if n <= 6 else 24)
    for i, t0 in enumerate(times):
        if i:
            _segment(c, at(i - 1), at(i), t0 - draw, draw, dot)
    for i, t0 in enumerate(times):  # точки — поверх линии
        x, y = at(i)
        on = f"gte(t,{t0:.3f})"
        c.box(x - mark // 2, y - mark // 2, mark, mark, ACCENT, enable=on)
        c.box(x - mark // 4, y - mark // 4, mark // 2, mark // 2, DARK, enable=on)
        c.text(_value(data[i], unit), vsize, f"{x}-text_w/2", y - mark - round(vsize * 1.2), alpha=appear(t0))
        c.text(data[i]["label"], lsize, f"{x}-text_w/2", base + c.px(16), color=MUTED, alpha=appear(t0))


def flow(c: Canvas, scene: dict, seconds: float) -> None:
    top = c.heading(scene["title"])
    steps_ = scene["steps"]
    n = len(steps_)
    times = reveal_times(n, seconds)
    size = c.px(40 if n <= 3 else 34)
    arrow = c.px(80)
    room = c.bottom - top
    if c.wide:
        bw = round((c.w - 2 * c.margin - (n - 1) * arrow) / n)
        bh = min(c.px(280), room)
        boxes = [(c.margin + i * (bw + arrow), top + (room - bh) // 2) for i in range(n)]
        sign = "→"
    else:
        bw = c.w - 2 * c.margin
        bh = min(c.px(170), round((room - (n - 1) * arrow) / n))
        boxes = [(c.margin, top + i * (bh + arrow)) for i in range(n)]
        sign = "↓"
    border = c.px(3)
    for i, ((bx, by), text, t0) in enumerate(zip(boxes, steps_, times, strict=True)):
        on = f"gte(t,{t0:.3f})"
        c.box(bx, by, bw, bh, PANEL, enable=on)
        c.box(bx, by, bw, bh, "white@0.25", enable=on, thick=str(border))
        # Рамка акцента — на последнем появившемся шаге; у последнего — до конца слайда.
        c.box(bx, by, bw, bh, ACCENT, enable=window(t0, times[i + 1] if i + 1 < n else None), thick=str(border * 2))
        label = wrap(text, c.chars(bw - 2 * c.px(24), size), 3)
        y = f"{by}+({bh}-text_h)/2"
        c.text(label, size, f"{bx}+({bw}-text_w)/2", y, alpha=appear(t0))
        if i + 1 < n:
            asize = c.px(64)
            if c.wide:
                ax, ay = f"{bx + bw + arrow // 2}-text_w/2", f"{by + bh // 2}-text_h/2"
            else:
                ax, ay = f"{bx + bw // 2}-text_w/2", f"{by + bh + arrow // 2}-text_h/2"
            c.text(sign, asize, ax, ay, color=ACCENT, alpha=appear(times[i + 1] - 0.3, 0.3))


def compare(c: Canvas, scene: dict, seconds: float) -> None:
    top = c.heading(scene["title"])
    left, right = scene["left"], scene["right"]
    gap = c.px(70)
    cw = (c.w - 2 * c.margin - gap) // 2
    ch = c.bottom - top
    hsize, isize = c.px(46 if c.wide else 40), c.px(38 if c.wide else 32)
    pad = c.px(36)
    sides = [
        (c.margin, left, left.get("title") or "Было", BAD, "✕"),
        (c.margin + cw + gap, right, right.get("title") or "Стало", GOOD, "✓"),
    ]
    rows = max(len(left["items"]), len(right["items"]))
    order = [("head", 0, 0), ("head", 1, 0)]
    for r in range(rows):
        order += [("item", s, r) for s, side in enumerate((left, right)) if r < len(side["items"])]
    times = dict(zip(order, reveal_times(len(order), seconds), strict=True))
    for s, (x, side, title, color, mark) in enumerate(sides):
        t0 = times[("head", s, 0)]
        on = f"gte(t,{t0:.3f})"
        c.box(x, top, cw, ch, PANEL, enable=on)
        c.box(x, top, cw, c.px(8), color, enable=on)
        c.text(
            wrap(title, c.chars(cw - 2 * pad, hsize), 1),
            hsize,
            c.slide_x(x + pad, t0, c.px(40)),
            top + pad,
            color=color,
            alpha=appear(t0),
        )
        y = top + pad + line_h(hsize) + c.px(20)
        for r, item in enumerate(side["items"]):
            text = wrap(f"{mark} {item}", c.chars(cw - 2 * pad, isize), 3)
            ti = times[("item", s, r)]
            c.text(text, isize, x + pad, c.slide_y(y, ti, c.px(24)), alpha=appear(ti))
            y += text_h(text, isize) + c.px(18)
    vs = c.px(44)
    pad_vs = c.px(22)
    c.parts.append(
        f"drawtext={'fontfile=' + quote(c.font) + ':' if c.font else ''}text=VS:fontsize={vs}:fontcolor={DARK}"
        f":box=1:boxcolor={ACCENT}:boxborderw={pad_vs}:x='(w-text_w)/2':y='{top + ch // 2}-text_h/2'"
        f":alpha='{appear(times[('head', 1, 0)], 0.3)}'"
    )


def quote_scene(c: Canvas, scene: dict, seconds: float) -> None:
    size = c.px(58 if c.wide else 52)
    text = wrap(scene["text"], c.chars(c.w - 2 * c.margin, size), 5)
    author = f"— {scene['author']}" if scene.get("author") else ""
    asize = c.px(40)
    mark = c.px(320)
    block = round(mark * 0.5) + text_h(text, size) + (line_h(asize) + c.px(50) if author else 0)
    y = max(c.top, (c.top + c.bottom) // 2 - block // 2)
    c.text("“", mark, c.margin - c.px(8), y - round(mark * 0.2), color=ACCENT, alpha=appear(0.1, 0.3))
    ty = y + round(mark * 0.5)
    span = min(max(seconds * 0.55, 0.8), len(text.split()) * 0.3)
    c.words(text, size, c.margin, ty, 0.35, span)
    ay = ty + text_h(text, size) + c.px(50)
    if author:
        c.text(author, asize, c.margin, ay, color=MUTED, alpha=appear(0.35 + span, 0.5))


MONO_FONTS = (
    "/System/Library/Fonts/Menlo.ttc",
    "/System/Library/Fonts/Monaco.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
)


def mono_font() -> str:
    return next((f for f in MONO_FONTS if Path(f).is_file()), "")


def chapter(c: Canvas, scene: dict, seconds: float) -> None:
    """Глава: «Глава N» мелко, крупное название по словам, под ним подзаголовок и полоса акцента."""
    kicker = f"Глава {scene['number']}" if scene.get("number") else "Глава"
    ksize, size, ssize = c.px(40), c.px(104 if c.wide else 86), c.px(42)
    text = wrap(scene["title"], c.chars(c.w - 2 * c.margin, size), 3)
    subtitle = wrap(scene.get("subtitle") or "", c.chars(c.w - 2 * c.margin, ssize), 2) if scene.get("subtitle") else ""
    block = line_h(ksize) + c.px(24) + text_h(text, size) + (c.px(36) + text_h(subtitle, ssize) if subtitle else 0)
    y = max(c.top, (c.top + c.bottom) // 2 - block // 2)
    c.text(kicker.upper(), ksize, c.slide_x(c.margin, 0.1, c.px(40)), y, color=ACCENT, alpha=appear(0.1, 0.4))
    ty = y + line_h(ksize) + c.px(24)
    span = min(max(seconds * 0.4, 0.6), len(text.split()) * 0.35)
    c.words(text, size, c.margin, ty, 0.35, span)
    bar_y = ty + text_h(text, size) + c.px(18)
    c.grow_right(c.margin, bar_y, c.w - 2 * c.margin, c.px(6), ACCENT, 0.35 + span, 0.8, steps=12)
    if subtitle:
        c.text(subtitle, ssize, c.margin, bar_y + c.px(30), color=MUTED, alpha=appear(0.6 + span, 0.5))


def code_scene(c: Canvas, scene: dict, seconds: float) -> None:
    """Код: тёмная панель, строки набираются по очереди, текущая подсвечена, номера строк слева."""
    top = c.heading(scene["title"]) if scene.get("title") else c.top
    lines = scene["code"].split("\n")
    mono = mono_font()
    size = c.px(40 if c.wide else 34)
    width = c.w - 2 * c.margin
    pad, gutter = c.px(28), c.px(70)
    while True:
        row = line_h(size)
        height = row * len(lines) + 2 * pad
        fits_w = max(len(line) for line in lines) * size * 0.62 <= width - gutter - 2 * pad
        if (top + height <= c.bottom and fits_w) or size <= c.px(20):
            break
        size = round(size * 0.9)
    row = line_h(size)
    height = row * len(lines) + 2 * pad
    c.box(c.margin, top, width, height, "black@0.4", enable="gte(t,0.2)")
    c.box(c.margin, top, c.px(8), height, ACCENT, enable="gte(t,0.2)")
    if scene.get("language"):
        c.text(
            scene["language"],
            c.px(28),
            f"{c.margin + width - pad}-text_w",
            top - c.px(40),
            color=MUTED,
            alpha=appear(0.2),
        )
    font = f"fontfile={quote(mono)}:" if mono else (f"fontfile={quote(c.font)}:" if c.font else "")
    times = reveal_times(len(lines), seconds, start=0.4, tail=max(TAIL_S, seconds * 0.25))
    for i, (line, t0) in enumerate(zip(lines, times, strict=True)):
        y = top + pad + i * row
        nxt = times[i + 1] if i + 1 < len(times) else None
        if line.strip():
            c.box(c.margin + c.px(8), y - c.px(4), width - c.px(8), row, "white@0.06", enable=window(t0, nxt))
        for text, x, color in ((str(i + 1), c.margin + pad, MUTED), (line or " ", c.margin + gutter + pad, INK)):
            part = (
                f"drawtext={font}textfile={quote(c.file(text))}:expansion=none:fontsize={size}"
                f":fontcolor={color}:x={x}:y={y}:alpha='{appear(t0, 0.25)}'"
            )
            c.parts.append(part)


def summary(c: Canvas, scene: dict, seconds: float) -> None:
    """Итог: пункты с галочками появляются по очереди."""
    y = c.heading(scene["title"]) if scene.get("title") else c.top
    items = scene["items"]
    size, gap = c.px(46 if c.wide else 42), c.px(26)
    mark = c.px(56)
    width = c.w - 2 * c.margin - mark - c.px(30)
    while True:
        texts = [wrap(item, c.chars(width, size), 2) for item in items]
        heights = [max(text_h(t, size), mark) for t in texts]
        if y + sum(heights) + gap * (len(items) - 1) <= c.bottom or size <= c.px(26):
            break
        size = round(size * 0.9)
    times = reveal_times(len(items), seconds)
    tx = c.margin + mark + c.px(30)
    for text, height, t0 in zip(texts, heights, times, strict=True):
        on = f"gte(t,{t0:.3f})"
        my = y + (height - mark) // 2
        ty = y + (height - text_h(text, size)) // 2
        c.box(c.margin, my, mark, mark, ACCENT, enable=on)
        c.text(
            "✓", round(mark * 0.8), f"{c.margin + mark // 2}-text_w/2", my + round(mark * 0.08), color=DARK, enable=on
        )
        c.text(text, size, c.slide_x(tx, t0, c.px(50)), ty, alpha=appear(t0))
        y += height + gap


SCENES = {
    "chapter": chapter,
    "code": code_scene,
    "summary": summary,
    "bullets": bullets,
    "stat": stat,
    "flow": flow,
    "compare": compare,
    "quote": quote_scene,
}


def draw(c: Canvas, scene: dict, seconds: float) -> str:
    """Фильтры сцены одной цепочкой (через запятую) поверх фона."""
    kind = scene.get("kind") or "bullets"
    if kind == "chart":
        (line_chart if scene.get("chart") == "line" else bar_chart)(c, scene, seconds)
    else:
        SCENES.get(kind, bullets)(c, scene, seconds)
    return ",".join(c.parts)
