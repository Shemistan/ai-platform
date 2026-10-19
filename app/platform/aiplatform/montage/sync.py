"""Сдвиг записи экрана относительно головы по звуку — без numpy, огибающие громкости.

offset — секунды: кадр экрана для момента t головы — screen(t + offset). Экран
начали писать на 2 с позже головы → offset = −2. Ищется максимум корреляции огибающих
сначала грубо (кадр 100 мс, ±MAX_LAG_S), затем точно (10 мс) около найденного.
"""

from __future__ import annotations

import array
import math
import subprocess
from pathlib import Path

RATE = 8000
WINDOW_S = 240.0  # сколько записи сравнивать
MAX_LAG_S = 60.0
MIN_SCORE = 0.3  # ниже — совпадения нет: сдвиг 0, человек поправит


def pcm(ffmpeg: str, source: Path, seconds: float = WINDOW_S) -> array.array:
    argv = [ffmpeg, "-v", "error", "-t", str(seconds), "-i", str(source), "-vn", "-ac", "1", "-ar", str(RATE),
            "-f", "s16le", "-"]  # fmt: skip
    data = subprocess.run(argv, capture_output=True, check=True).stdout
    samples = array.array("h")
    samples.frombytes(data[: len(data) // 2 * 2])
    return samples


def envelope(samples: array.array, frame_s: float) -> list[float]:
    step = max(1, int(RATE * frame_s))
    out = []
    for i in range(0, len(samples) - step + 1, step):
        chunk = samples[i : i + step]
        out.append(math.sqrt(sum(x * x for x in chunk) / step))
    mean = sum(out) / len(out) if out else 0.0
    return [x - mean for x in out]


def _score(a: list[float], b: list[float], lag: int) -> float:
    """Корреляция a[i] и b[i + lag] по перекрытию, нормированная."""
    lo, hi = max(0, -lag), min(len(a), len(b) - lag)
    if hi - lo < 10:
        return -1.0
    num = sa = sb = 0.0
    for i in range(lo, hi):
        x, y = a[i], b[i + lag]
        num += x * y
        sa += x * x
        sb += y * y
    return num / math.sqrt(sa * sb) if sa and sb else -1.0


def best_lag(a: list[float], b: list[float], lags: range) -> tuple[int, float]:
    return max(((lag, _score(a, b, lag)) for lag in lags), key=lambda p: p[1])


def find_offset(head: array.array, screen: array.array, max_lag_s: float = MAX_LAG_S) -> tuple[float, float]:
    """(offset, оценка 0…1). Грубо по 100 мс, потом точно по 10 мс."""
    coarse_a, coarse_b = envelope(head, 0.1), envelope(screen, 0.1)
    span = int(max_lag_s / 0.1)
    lag, _ = best_lag(coarse_a, coarse_b, range(-span, span + 1))
    fine_a, fine_b = envelope(head, 0.01), envelope(screen, 0.01)
    lag_fine, score = best_lag(fine_a, fine_b, range(lag * 10 - 12, lag * 10 + 13))
    return round(lag_fine * 0.01, 3), round(max(score, 0.0), 3)


def detect(ffmpeg: str, head: Path, screen: Path) -> dict:
    """{"offset", "score", "method"}: по звуку или «нет звука» (сдвиг 0)."""
    try:
        a, b = pcm(ffmpeg, head), pcm(ffmpeg, screen)
    except subprocess.CalledProcessError:
        return {"offset": 0.0, "score": 0.0, "method": "none"}
    if len(b) < RATE or not any(b[i] for i in range(0, len(b), 97)):
        return {"offset": 0.0, "score": 0.0, "method": "none"}
    offset, score = find_offset(a, b)
    if score < MIN_SCORE:
        return {"offset": 0.0, "score": score, "method": "weak"}
    return {"offset": offset, "score": score, "method": "audio"}
