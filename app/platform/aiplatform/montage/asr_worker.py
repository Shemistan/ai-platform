"""Воркер расшифровки монтажа: wav 16 кГц моно → слова со временем (JSON).

Запускается в окружении mlx-audio (tools/setup-model.sh, `make models M=<модель>`),
не в окружении платформы: платформу не импортирует, тяжёлое (mlx, numpy) —
внутри функций, чтобы чистую часть проверяли тесты платформы.

    python asr_worker.py --kind whisper --model <каталог> --audio a.wav --out words.json \
        --language ru [--splits 290.1,581.7] [--hotwords "Claude Code|ffmpeg"]
    python asr_worker.py --kind qwen3 --model <каталог> --aligner <каталог> --language Russian …

Звук режется по --splits (их считает платформа: в тишине, кусок ≤ 5 мин).
Прогресс — `PROGRESS <процент> <этап>` в stdout после каждого куска. Итог:
{"language", "duration", "elapsed_s", "words": [{"w", "s", "e"}]}.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import unicodedata
import wave
from itertools import pairwise
from pathlib import Path

SAMPLE_RATE = 16000


def progress(percent: float, stage: str) -> None:
    print(f"PROGRESS {percent:.1f} {stage}", flush=True)


def spans(duration: float, splits: list[float]) -> list[tuple[float, float]]:
    """Куски [начало, конец) по точкам разреза; точки вне звука и повторы отбрасываются."""
    points = sorted({round(s, 3) for s in splits if 0 < s < duration})
    edges = [0.0, *points, duration]
    return [(a, b) for a, b in pairwise(edges) if b - a > 0.01]


def _kept(ch: str) -> bool:
    return ch == "'" or unicodedata.category(ch)[0] in "LN"


def clean(token: str) -> str:
    """Как ForceAlignProcessor.clean_token: только буквы, цифры и апостроф."""
    return "".join(ch for ch in token if _kept(ch))


def restore_punctuation(text: str, items: list[tuple[str, float, float]]) -> list[dict]:
    """Слова выравнивателя (без пунктуации) → слова текста ASR с их пунктуацией и временем.

    Выравниватель режет текст по пробелам и чистит каждое слово; идём по словам текста
    по порядку: слово, от которого после чистки ничего не осталось («—»), приклеивается
    к соседу. Не сошлось — берём слово выравнивателя как есть.
    """
    raw = text.split()
    words: list[dict] = []
    i = 0
    pending = ""  # пунктуация без букв, ждущая следующего слова
    for token, start, end in items:
        piece = None
        while i < len(raw):
            candidate = raw[i]
            i += 1
            if not clean(candidate):
                if words:
                    words[-1]["w"] += " " + candidate
                else:
                    pending += candidate + " "
                continue
            piece = candidate if clean(candidate) == token else None
            break
        words.append({"w": pending + (piece or token), "s": round(start, 3), "e": round(max(end, start), 3)})
        pending = ""
    while i < len(raw) and words:  # хвост пунктуации
        if not clean(raw[i]):
            words[-1]["w"] += " " + raw[i]
        i += 1
    return words


def read_wav(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as fh:
        if fh.getframerate() != SAMPLE_RATE or fh.getnchannels() != 1 or fh.getsampwidth() != 2:
            raise SystemExit(f"нужен wav 16 кГц моно 16 бит: {path}")
        data = fh.readframes(fh.getnframes())
    return np.frombuffer(data, dtype="<i2").astype("float32") / 32768.0


def whisper_words(model, audio, language: str, hotwords: list[str]) -> list[dict]:
    result = model.generate(
        audio,
        language=language,
        word_timestamps=True,
        condition_on_previous_text=False,  # меньше зацикливаний на длинной записи
        hallucination_silence_threshold=2.0,
        hotwords=hotwords or None,
    )
    words = []
    for segment in result.segments or []:
        for item in segment.get("words") or []:
            text = str(item["word"]).strip()
            if text:
                words.append({"w": text, "s": round(float(item["start"]), 3), "e": round(float(item["end"]), 3)})
    return words


def qwen_words(asr, aligner, audio, language: str, hotwords: list[str]) -> list[dict]:
    result = asr.generate(audio, language=language, hotwords=hotwords or None)
    text = (result.text or "").strip()
    if not text:
        return []
    aligned = aligner.generate(audio, text, language=language)
    items = [(item.text, float(item.start_time), float(item.end_time)) for item in aligned.items]
    return restore_punctuation(text, items)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--kind", choices=("whisper", "qwen3"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--aligner")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--language", default="ru")
    parser.add_argument("--splits", default="")
    parser.add_argument("--hotwords", default="")
    args = parser.parse_args(argv)
    if args.kind == "qwen3" and not args.aligner:
        parser.error("для qwen3 нужен --aligner")

    started = time.time()
    progress(0, "загрузка модели")
    from mlx_audio.stt.utils import load

    audio = read_wav(Path(args.audio))
    duration = len(audio) / SAMPLE_RATE
    splits = [float(s) for s in args.splits.split(",") if s.strip()]
    hotwords = [w for w in args.hotwords.split("|") if w.strip()]
    model = load(args.model)
    aligner = load(args.aligner) if args.kind == "qwen3" else None
    pieces = spans(duration, splits)
    words: list[dict] = []
    for n, (a, b) in enumerate(pieces, 1):
        chunk = audio[int(a * SAMPLE_RATE) : int(b * SAMPLE_RATE)]
        found = (
            whisper_words(model, chunk, args.language, hotwords)
            if args.kind == "whisper"
            else qwen_words(model, aligner, chunk, args.language, hotwords)
        )
        for word in found:  # время куска → время всей записи
            start = min(max(word["s"], 0.0), b - a)
            end = min(max(word["e"], start), b - a)
            word["s"], word["e"] = round(start + a, 3), round(end + a, 3)
        words.extend(found)
        progress(100 * b / duration if duration else 100, f"кусок {n} из {len(pieces)}")
    out = {"language": args.language, "duration": round(duration, 3), "elapsed_s": round(time.time() - started, 1),
           "words": words}  # fmt: skip
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
