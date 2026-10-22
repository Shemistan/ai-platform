"""Когда звучит каждая фраза озвучки: по паузам в самом звуке, а не по доле слов (BE-016).

Доля слов ошибается на секунды: речь идёт неравномерно — паузы между абзацами,
заголовки, темп модели. Замер на выпусках: статья по доле слов опаздывала до 9 с,
дайджест — до 5 с. Паузы в звуке точнее: narrator ставит тишину между абзацами
(0,7 с, перед заголовком 1,2 с), между фразами модель тоже замолкает.

Как: границы абзацев сопоставляются длинным паузам — порог от пауз этой же
озвучки (темп и движок у всех разные), фразы внутри уже привязанного абзаца —
коротким паузам. И то и другое — по порядку, динамическим программированием:
сумма расхождений с оценкой минимальна, лишняя пауза бесплатна, граница без
паузы — штраф. Оценка второго прохода — по словам между найденными абзацами.
Пауз нет вовсе — остаётся доля слов, как раньше.
"""

from __future__ import annotations

from aiplatform.studio import slides

SHORT_S = 0.3  # пауза между фразами; короче — стык согласных, а не граница
LONG_MIN_S, LONG_MAX_S = 0.3, 1.0  # порог паузы между абзацами — в этих пределах
LONG_SHARE = 0.75  # порог — доля от самой короткой из «абзацных» пауз
PARA_SKIP = 10.0  # штраф за абзац без паузы: narrator ставит её всегда
PARA_WINDOW_S = 20.0  # дальше от оценки пауза абзацу не подходит
PARA_BONUS = 20.0  # секунд расхождения за секунду паузы сверх порога: пауза абзаца — самая длинная
SENT_SKIP = 3.0  # фраза без паузы бывает: модель сливает фразы
SENT_WINDOW_S = 8.0
PAUSE_BONUS = 5.0  # секунд расхождения за секунду паузы: между фразами пауза длиннее, чем на запятой
EDGE_S = 0.3  # пауза фразы не у самых краёв абзаца
LEAD_S = 0.2  # слайд — на столько раньше конца паузы, но не раньше её начала


VOWELS = set("аеёиоуыэюяaeiouy")
COMMA_SYLLABLES = 2.0  # пауза на запятой, тире, двоеточии — как пара слогов


def weight(text: str) -> float:
    """Сколько «звучит» фраза: слоги (гласные) и паузы на знаках внутри — точнее, чем число слов."""
    low = text.lower()
    syllables = sum(ch in VOWELS for ch in low)
    stops = sum(low.count(ch) for ch in ",;:—–()")
    return float(max(syllables, len(text.split()))) + COMMA_SYLLABLES * stops


def match(
    expected: list[float], points: list[float], skip: float, window: float, bonus: list[float] | None = None
) -> list[int | None]:
    """Каждой границе — индекс точки или None, по порядку и без повторов: минимум суммы |точка − оценка|.

    Пропуск точки бесплатен, границы — skip; точка дальше window от оценки не подходит.
    bonus — скидка точке (длинная пауза — скорее граница фразы, чем запятая)."""
    bonus = bonus or [0.0] * len(points)
    n, m = len(expected), len(points)
    inf = float("inf")
    cost = [[0.0] * (m + 1)] + [[inf] * (m + 1) for _ in range(n)]
    back: list[list[tuple[int, int, bool]]] = [[(0, 0, False)] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        for j in range(m + 1):
            best, how = cost[i - 1][j] + skip, (i - 1, j, False)
            if j and cost[i][j - 1] < best:
                best, how = cost[i][j - 1], (i, j - 1, False)
            if j:
                gap = abs(points[j - 1] - expected[i - 1])
                price = cost[i - 1][j - 1] + gap - bonus[j - 1]
                if gap <= window and price < best:
                    best, how = price, (i - 1, j - 1, True)
            cost[i][j], back[i][j] = best, how
    out: list[int | None] = [None] * n
    i, j = n, m
    while i > 0:
        pi, pj, used = back[i][j]
        if used:
            out[i - 1] = pj
        i, j = pi, pj
    return out


def long_pause(silences: list[tuple[float, float]], boundaries: int) -> float:
    """С какой длины пауза — между абзацами: абзацев boundaries + 1, значит, таких пауз не меньше boundaries."""
    lengths = sorted((b - a for a, b in silences), reverse=True)
    if not boundaries or not lengths:
        return LONG_MAX_S
    shortest = lengths[min(boundaries, len(lengths)) - 1]
    return min(max(shortest * LONG_SHARE, LONG_MIN_S), LONG_MAX_S)


def _between(weights: list[float], known: dict[int, float], total: float, total_s: float) -> list[float]:
    """Время каждой фразы: найденные — как есть, остальные — по слогам между соседними найденными."""
    keys = sorted({0, *known})
    out = []
    for k, w in enumerate(weights):
        if k in known:
            out.append(known[k])
            continue
        lo = max(x for x in keys if x <= k)
        hi = min((x for x in keys if x > k), default=None)
        t_lo, w_lo = known.get(lo, 0.0), weights[lo]
        t_hi, w_hi = (known[hi], weights[hi]) if hi is not None else (total_s, total)
        out.append(t_lo + (t_hi - t_lo) * ((w - w_lo) / (w_hi - w_lo) if w_hi > w_lo else 0.0))
    return out


def phrase_starts(
    paras: list[str], total_s: float, silences: list[tuple[float, float]]
) -> dict[tuple[int, int], float]:
    """С какой секунды звучит каждая фраза: {(абзац, номер фразы): секунда}. Без пауз — по слогам."""
    marks, before = [], 0.0  # (абзац, фраза), сколько звучит до неё
    for p, para in enumerate(paras):
        for s, sent in enumerate(slides.sentences(para)):
            marks.append(((p, s), before))
            before += weight(sent)
    if not marks:
        return {}
    total = before or 1.0
    weights = [w for _, w in marks]
    heads = [k for k, ((_, s), _) in enumerate(marks) if s == 0 and k]
    inner = [(b, b - a) for a, b in silences if 0 < b < total_s]  # (конец паузы, её длина)
    floor = long_pause([(b - d, b) for b, d in inner], len(heads))
    longs = [(b, d) for b, d in inner if d >= floor]
    shorts = [(b, d) for b, d in inner if d >= SHORT_S]
    found: dict[int, tuple[float, float]] = {}
    times = _between(weights, {}, total, total_s)
    for _ in range(2):  # второй проход: оценка — между уже найденными абзацами
        bonus = [PARA_BONUS * (d - floor) for _, d in longs]
        picked = match([times[k] for k in heads], [b for b, _ in longs], PARA_SKIP, PARA_WINDOW_S, bonus)
        found = {heads[i]: longs[j] for i, j in enumerate(picked) if j is not None}
        times = _between(weights, {k: b for k, (b, _) in found.items()}, total, total_s)
    bounds = [0, *heads, len(marks)]
    for lo, hi in zip(bounds, bounds[1:], strict=False):
        inside = list(range(lo + 1, hi))
        end = times[hi] if hi < len(marks) else total_s
        near = [(b, d) for b, d in shorts if times[lo] + EDGE_S < b < end - EDGE_S]
        bonus = [PAUSE_BONUS * (d - SHORT_S) for _, d in near]
        picked = match([times[k] for k in inside], [b for b, _ in near], SENT_SKIP, SENT_WINDOW_S, bonus)
        found |= {k: near[j] for k, j in zip(inside, picked, strict=True) if j is not None}
    # Слайд меняется чуть раньше конца паузы: тихое начало слова silencedetect считает тишиной.
    for k, (b, d) in found.items():
        times[k] = b - min(LEAD_S, d)
    return {key: round(t, 3) for (key, _), t in zip(marks, times, strict=True)}
