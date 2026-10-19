"""Имитация воркера расшифровки монтажа (montage/asr_worker.py) для демо и тестов.

Те же аргументы и протокол: `PROGRESS …` в stdout, JSON слов в --out. Текст —
демо-обзор по кругу (с паузой, «э-э», повтором и командами в кадр), слова
раскладываются по длительности wav, пока звук не кончится.

Окружение: FAKE_ASR_FAIL=1 — упасть; FAKE_ASR_STEP_DELAY — пауза на кусок, с;
FAKE_ASR_TEXT — свой текст вместо демо.
"""

from __future__ import annotations

import json
import os
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from aiplatform.montage import asr_worker  # noqa: E402 — путь пакета добавлен строкой выше

TEXT = (
    "Привет, это обзор недели. Э-э, начнём с главного. Claude Code получил новый режим, "
    "новый режим монтажа. Покажи экран. Здесь видно, как ffmpeg режет запись на куски. "
    "Вырежи это. Дальше итоги и планы на следующую неделю."
)
WORD_S = 0.32
GAP_S = 0.06
SENTENCE_GAP_S = 0.7
LONG_PAUSE_S = 1.8  # после второго предложения: её должен ужать монтаж


def words_for(duration: float, text: str) -> list[dict]:
    tokens = text.split()
    out: list[dict] = []
    t = 0.3
    sentence = 0
    while tokens:
        for token in tokens:
            if t + WORD_S > duration - 0.2:
                return out
            out.append({"w": token, "s": round(t, 3), "e": round(t + WORD_S, 3)})
            t += WORD_S + GAP_S
            if token[-1] in ".!?":
                sentence += 1
                t += LONG_PAUSE_S if sentence % 7 == 2 else SENTENCE_GAP_S
    return out


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser()
    for name in ("--kind", "--model", "--audio", "--out"):
        parser.add_argument(name, required=True)
    for name in ("--aligner", "--language", "--splits", "--hotwords"):
        parser.add_argument(name, default="")
    args = parser.parse_args(argv)
    if os.environ.get("FAKE_ASR_FAIL"):
        print("fake asr: модель не загрузилась", file=sys.stderr)
        return 1
    with wave.open(args.audio, "rb") as fh:
        duration = fh.getnframes() / fh.getframerate()
    delay = float(os.environ.get("FAKE_ASR_STEP_DELAY") or 0)
    splits = [float(s) for s in args.splits.split(",") if s.strip()]
    pieces = asr_worker.spans(duration, splits)
    for n, (_, end) in enumerate(pieces, 1):
        time.sleep(delay)
        asr_worker.progress(100 * end / duration if duration else 100, f"кусок {n} из {len(pieces)}")
    words = words_for(duration, os.environ.get("FAKE_ASR_TEXT") or TEXT)
    out = {"language": args.language, "duration": round(duration, 3), "elapsed_s": 0.0, "words": words}
    Path(args.out).write_text(json.dumps(out, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
