"""Расшифровка монтажа: слова со временем, тишина из ffmpeg, точки разреза, сравнение текстов.

Слово — {"w": текст с пунктуацией, "s": начало, "e": конец} в секундах исходника.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

from aiplatform.montage.asr import MAX_CHUNK_S

SILENCE_DB = -35  # тише — тишина (голос у микрофона заметно громче)
SILENCE_MIN_S = 0.2
_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_END = re.compile(r"silence_end:\s*(\d+(?:\.\d+)?)")
_LATIN = re.compile(r"[a-z]")


def silence_argv(ffmpeg: str, audio: Path, noise_db: int = SILENCE_DB, min_s: float = SILENCE_MIN_S) -> list[str]:
    """ffmpeg, который печатает в stderr границы тишины и ничего не пишет на диск."""
    return [ffmpeg, "-hide_banner", "-nostats", "-i", str(audio),
            "-af", f"silencedetect=noise={noise_db}dB:d={min_s}", "-f", "null", "-"]  # fmt: skip


def parse_silences(lines: list[str], duration: float | None = None) -> list[tuple[float, float]]:
    """Строки silencedetect → [(начало, конец)]; тишина до конца файла закрывается длительностью."""
    out: list[tuple[float, float]] = []
    start: float | None = None
    for line in lines:
        if found := _START.search(line):
            start = max(0.0, float(found[1]))
        elif (found := _END.search(line)) and start is not None:
            out.append((round(start, 3), round(float(found[1]), 3)))
            start = None
    if start is not None and duration is not None and duration > start:
        out.append((round(start, 3), round(duration, 3)))
    return out


def split_points(duration: float, silences: list[tuple[float, float]], max_chunk: float = MAX_CHUNK_S) -> list[float]:
    """Где резать звук на куски ≤ max_chunk: середина самой длинной тишины во второй половине окна.

    Тишины нет — режем ровно по max_chunk (слово на стыке может разорваться — это редкость).
    """
    points: list[float] = []
    pos = 0.0
    while duration - pos > max_chunk:
        lo, hi = pos + max_chunk / 2, pos + max_chunk
        inside = [(b - a, (a + b) / 2) for a, b in silences if lo < (a + b) / 2 <= hi]
        cut = max(inside)[1] if inside else hi
        points.append(round(cut, 3))
        pos = cut
    return points


def gaps(words: list[dict], duration: float) -> list[tuple[int, float, float]]:
    """Промежутки без речи: (индекс слова после промежутка, начало, конец); последний — len(words)."""
    out = []
    prev = 0.0
    for i, word in enumerate(words):
        if word["s"] > prev:
            out.append((i, prev, word["s"]))
        prev = max(prev, word["e"])
    if duration > prev:
        out.append((len(words), prev, duration))
    return out


# ---------- сравнение текстов (make asr-compare) ----------


def normalize(text: str) -> list[str]:
    """Слова для сравнения: нижний регистр, ё → е, без пунктуации; дефис и апостроф внутри слова — пробел."""
    text = unicodedata.normalize("NFC", text.lower()).replace("ё", "е")
    text = re.sub(r"[^\w\s]|_", " ", text)
    return text.split()


def edit_ops(ref: list[str], hyp: list[str]) -> tuple[int, list[bool]]:
    """Расстояние Левенштейна по словам и, для каждого слова эталона, узнано ли оно точно."""
    n, m = len(ref), len(hyp)
    # Полная таблица ходов нужна для восстановления пути; хранится компактно — по строке байтов.
    prev = list(range(m + 1))
    moves: list[bytearray] = []
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        row = bytearray(m + 1)
        row[0] = 1  # удаление слова эталона
        r = ref[i - 1]
        for j in range(1, m + 1):
            sub = prev[j - 1] + (r != hyp[j - 1])
            dele = prev[j] + 1
            ins = cur[j - 1] + 1
            if sub <= dele and sub <= ins:
                cur[j], row[j] = sub, 0
            elif dele <= ins:
                cur[j], row[j] = dele, 1
            else:
                cur[j], row[j] = ins, 2
        moves.append(row)
        prev = cur
    hit = [False] * n
    i, j = n, m
    while i > 0:
        move = moves[i - 1][j] if j > 0 else 1
        if move == 0:
            hit[i - 1] = ref[i - 1] == hyp[j - 1]
            i, j = i - 1, j - 1
        elif move == 1:
            i -= 1
        else:
            j -= 1
    return prev[m], hit


def wer(ref: list[str], hyp: list[str]) -> float:
    if not ref:
        return 0.0 if not hyp else 1.0
    return edit_ops(ref, hyp)[0] / len(ref)


def latin_recall(ref: list[str], hyp: list[str]) -> tuple[int, int]:
    """Сколько латинских слов эталона (Claude, ffmpeg) узнано точно: (узнано, всего)."""
    _, hit = edit_ops(ref, hyp)
    latin = [i for i, w in enumerate(ref) if _LATIN.search(w)]
    return sum(hit[i] for i in latin), len(latin)


def words_in_silence(words: list[dict], silences: list[tuple[float, float]], share: float = 0.6) -> float:
    """Доля слов, у которых больше share длительности приходится на тишину: грубая мера точности времени."""
    if not words:
        return 0.0
    bad = 0
    for word in words:
        length = word["e"] - word["s"]
        if length <= 0:
            bad += 1
            continue
        quiet = sum(max(0.0, min(word["e"], b) - max(word["s"], a)) for a, b in silences)
        bad += quiet / length > share
    return bad / len(words)
