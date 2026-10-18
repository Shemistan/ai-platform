"""Монтажный лист: вырезки по индексам слов и паузы → сегменты исходника; карта «результат → исходник».

Вырезка — {"from", "to", "kind", "reason"}: слова from…to включительно (их выбирает LLM или
человек). Паузы ужимаются без LLM: промежуток длиннее max_pause становится стыком двух
сегментов, от паузы остаётся keep_pause (поровну до и после). Граница сегмента ставится в
тишину по silencedetect (Whisper ошибается во времени слов на десятые секунды), никогда —
внутрь соседнего слова.
"""

from __future__ import annotations

from dataclasses import dataclass, field

KINDS = {
    "filler": "паразит",
    "retake": "дубль",
    "slip": "оговорка",
    "command": "команда в кадр",
    "extra": "лишнее",
    "human": "вырезал человек",
    "check": "найдено проверкой",
}
LAYOUTS = {"head": "Голова", "screen": "Экран", "split": "Экран + голова"}
MAX_PAUSE_S = 0.6
KEEP_PAUSE_S = 0.25
MIN_SEGMENT_S = 0.2
SNAP_WINDOW_S = 0.3  # насколько тишина может «поправить» время слова


@dataclass
class Segment:
    start: float  # секунды исходника
    end: float
    layout: str
    first: int  # индексы слов
    last: int
    out_start: float = 0.0  # где сегмент начинается в результате

    @property
    def duration(self) -> float:
        return self.end - self.start

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "layout": self.layout,
                "first": self.first, "last": self.last, "out_start": round(self.out_start, 3)}  # fmt: skip


@dataclass
class Edl:
    segments: list[Segment] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    def to_source(self, t: float) -> float:
        """Время результата → время исходника."""
        for seg in self.segments:
            if t < seg.out_start + seg.duration or seg is self.segments[-1]:
                return seg.start + max(0.0, min(t - seg.out_start, seg.duration))
        return 0.0

    def as_list(self) -> list[dict]:
        return [s.as_dict() for s in self.segments]


def clean_cuts(cuts: object, n_words: int, default_kind: str = "extra") -> list[dict]:
    """Вырезки из ответа LLM или запроса человека: индексы в пределах, вид из KINDS, без пересечений."""
    out = []
    for item in cuts if isinstance(cuts, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            a, b = int(item.get("from")), int(item.get("to", item.get("from")))
        except (TypeError, ValueError):
            continue
        a, b = max(0, min(a, b)), min(n_words - 1, max(a, b))
        if a > b:
            continue
        kind = item.get("kind") if item.get("kind") in KINDS else default_kind
        reason = str(item.get("reason") or "").strip()[:300]
        out.append({"from": a, "to": b, "kind": kind, "reason": reason})
    out.sort(key=lambda c: (c["from"], c["to"]))
    merged: list[dict] = []
    for cut in out:  # пересекающиеся — в одну, вид и причина первой
        if merged and cut["from"] <= merged[-1]["to"] + 1 and cut["kind"] == merged[-1]["kind"]:
            merged[-1]["to"] = max(merged[-1]["to"], cut["to"])
        elif merged and cut["from"] <= merged[-1]["to"]:
            cut["from"] = merged[-1]["to"] + 1
            if cut["from"] <= cut["to"]:
                merged.append(cut)
        else:
            merged.append(cut)
    return merged


def clean_layout(marks: object, n_words: int, has_screen: bool) -> list[dict]:
    """Смены плана {"from": индекс слова, "layout"}; без «экрана» — только голова."""
    if not has_screen:
        return [{"from": 0, "layout": "head"}]
    out: dict[int, str] = {0: "head"}
    for item in marks if isinstance(marks, list) else []:
        if not isinstance(item, dict) or item.get("layout") not in LAYOUTS:
            continue
        try:
            at = int(item.get("from"))
        except (TypeError, ValueError):
            continue
        if 0 <= at < max(n_words, 1):
            out[at] = item["layout"]
    marks_sorted = sorted(out.items())
    result: list[dict] = []
    for at, layout in marks_sorted:  # подряд одинаковые — лишние
        if not result or result[-1]["layout"] != layout:
            result.append({"from": at, "layout": layout})
    return result


def layout_of(marks: list[dict], index: int) -> str:
    current = "head"
    for mark in marks:
        if mark["from"] > index:
            break
        current = mark["layout"]
    return current


def _silence_near(silences: list[tuple[float, float]], lo: float, hi: float, target: float):
    best = None
    for a, b in silences:
        if b < lo or a > hi:
            continue
        dist = 0.0 if a <= target <= b else min(abs(a - target), abs(b - target))
        if best is None or dist < best[0]:
            best = (dist, a, b)
    return best[1:] if best else None


def _start_of(words, i, silences, pad) -> float:
    s = words[i]["s"]
    prev_e = words[i - 1]["e"] if i > 0 else 0.0
    start = s - min(pad, max(0.0, s - prev_e) / 2)
    near = _silence_near(silences, max(prev_e, s - SNAP_WINDOW_S), s + SNAP_WINDOW_S / 2, s)
    if near:  # речь началась там, где кончилась тишина
        a, b = near
        start = max(a, b - pad, prev_e)
    return max(0.0, min(start, s))


def _end_of(words, j, silences, pad, duration) -> float:
    e = words[j]["e"]
    next_s = words[j + 1]["s"] if j + 1 < len(words) else duration
    end = e + min(pad, max(0.0, next_s - e) / 2)
    near = _silence_near(silences, e - SNAP_WINDOW_S / 2, min(next_s, e + SNAP_WINDOW_S), e)
    if near:  # речь кончилась там, где началась тишина
        a, b = near
        end = min(b, a + pad, next_s)
    return min(duration, max(end, e))


def build(
    words: list[dict],
    duration: float,
    cuts: list[dict],
    layout: list[dict],
    silences: list[tuple[float, float]] | None = None,
    *,
    max_pause: float = MAX_PAUSE_S,
    keep_pause: float = KEEP_PAUSE_S,
) -> Edl:
    silences = silences or []
    keep = [True] * len(words)
    for cut in cuts:
        for i in range(cut["from"], cut["to"] + 1):
            keep[i] = False
    # Прогоны оставленных слов: разрыв — вырезка, длинная пауза или смена плана.
    runs: list[tuple[int, int, str]] = []
    for i, word in enumerate(words):
        if not keep[i]:
            continue
        mode = layout_of(layout, i)
        if runs:
            a, b, m = runs[-1]
            joined = b == i - 1 and word["s"] - words[b]["e"] <= max_pause
            if joined and m == mode:
                runs[-1] = (a, i, m)
                continue
            if joined:  # смена плана посреди речи — стык без паузы
                runs.append((i, i, mode))
                continue
        runs.append((i, i, mode))
    pad = keep_pause / 2
    edl = Edl()
    for n, (a, b, mode) in enumerate(runs):
        contiguous_before = n > 0 and runs[n - 1][1] == a - 1 and words[a]["s"] - words[a - 1]["e"] <= max_pause
        # Продолжение предыдущего (смена плана посреди речи) — граница там же, где он кончился.
        start = edl.segments[-1].end if contiguous_before else _start_of(words, a, silences, pad)
        contiguous_after = (
            n + 1 < len(runs) and runs[n + 1][0] == b + 1 and words[b + 1]["s"] - words[b]["e"] <= max_pause
        )
        if contiguous_after:
            end = (words[b]["e"] + words[b + 1]["s"]) / 2
        else:
            end = _end_of(words, b, silences, pad, duration)
        if edl.segments:
            start = max(start, edl.segments[-1].end)
        if end - start < MIN_SEGMENT_S:
            end = min(duration, start + MIN_SEGMENT_S)
        if end <= start:
            continue
        edl.segments.append(Segment(start, end, mode, a, b))
    total = 0.0
    for seg in edl.segments:
        seg.out_start = total
        total += seg.duration
    return edl


def output_words(words: list[dict], edl: Edl) -> list[dict]:
    """Слова результата со временем результата (для субтитров и проверки) и индексом в исходнике."""
    out = []
    for seg in edl.segments:
        for i in range(seg.first, seg.last + 1):
            w = words[i]
            s = max(w["s"], seg.start) - seg.start + seg.out_start
            e = min(w["e"], seg.end) - seg.start + seg.out_start
            out.append({"w": w["w"], "s": round(s, 3), "e": round(max(e, s), 3), "src": i})
    return out


def summary(words: list[dict], duration: float, cuts: list[dict], edl: Edl) -> dict:
    by_kind: dict[str, int] = {}
    for cut in cuts:
        by_kind[cut["kind"]] = by_kind.get(cut["kind"], 0) + 1
    return {
        "source_s": round(duration, 1),
        "result_s": round(edl.duration, 1),
        "cuts": len(cuts),
        "cut_words": sum(c["to"] - c["from"] + 1 for c in cuts),
        "by_kind": by_kind,
        "segments": len(edl.segments),
        "words": len(words),
    }
