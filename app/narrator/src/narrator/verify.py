"""Проверка озвучки обратным распознаванием: клип блока → faster-whisper → сверка.

Модель синтеза иногда глотает или коверкает слова. Слушать час аудио агент
не может, поэтому каждый клип распознаётся и сравнивается с текстом по
словам. Блоки с низким совпадением идут в check.md: их стоит переписать
(разнести названия, упростить слово, поставить ударение) и переозвучить.

Распознавание тоже ошибается. Цифры в распознанном тексте переводятся в слова,
но падеж может не совпасть; латиница («API», «Python») — это Whisper узнал
термин, а не ошибка озвучки. Отчёт — повод перечитать блок, а не приговор.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from narrator import models
from narrator.script import ACUTE
from narrator.synth import MANIFEST

WHISPER_MODEL = "small"
# Коммит Systran/faster-whisper-small на huggingface.co (SEC-011); тот же — в tools/fetch-models.sh.
WHISPER_REVISION = "536b0662742c02347bc0e980a01041f333bce120"
DEFAULT_THRESHOLD = 0.85

_WORD = re.compile(r"[a-zа-я0-9]+")


def words(text: str) -> list[str]:
    text = text.lower().replace("ё", "е").replace(ACUTE, "")
    return _WORD.findall(text)


def digits_to_words(tokens: list[str]) -> list[str]:
    try:
        from num2words import num2words
    except ImportError:
        return tokens
    out: list[str] = []
    for t in tokens:
        if t.isdigit() and len(t) < 16:
            out.extend(words(num2words(int(t), lang="ru")))
        else:
            out.append(t)
    return out


@dataclass
class BlockCheck:
    line: int
    text: str
    heard: str
    score: float
    missed: list[str]


_NUM_STEMS: list[tuple[str, str]] | None = None


def _num_stems() -> list[tuple[str, str]]:
    """(основа, ключ) для слов-чисел: «три», «трет(ий)» → «#3»; длинные основы первыми."""
    global _NUM_STEMS
    if _NUM_STEMS is None:
        _NUM_STEMS = []
        try:
            from num2words import num2words
        except ImportError:
            return _NUM_STEMS
        base = [*range(20), *range(20, 100, 10), *range(100, 1000, 100), 1000, 10**6, 10**9]
        pairs = [("две", "#2"), ("одна", "#1"), ("одно", "#1")]
        for n in base:
            for kind in ("cardinal", "ordinal"):
                w = words(num2words(n, lang="ru", to=kind))[-1]
                pairs.append((w if len(w) <= 4 else w[:-2], f"#{n}"))
        _NUM_STEMS = sorted(set(pairs), key=lambda p: len(p[0]), reverse=True)
    return _NUM_STEMS


def number_key(word: str) -> str:
    """Слово-число в любом падеже, количественное или порядковое, → ключ числа.

    Whisper пишет «Часть 5» вместо «Часть пятая», «2026 года» вместо «двадцать
    шестого»: без этого каждый такой заголовок попадал в check.md как ошибка.
    """
    for stem, key in _num_stems():
        if word == stem or (
            len(stem) >= 3 and word.startswith(stem) and len(word) <= len(stem) + 4
        ):
            return key
    return word


def _spoken(heard: str) -> str:
    """Латиницу и цифры в распознанном читаем так же, как нормализатор читал их в тексте.

    Whisper пишет «GitHub» и «API», а в тексте для озвучки стоит «гитхаб» и
    «эй пи ай»: без этого каждый термин считался бы искажённым.
    """
    if not re.search(r"[A-Za-z\d%$]", heard):
        return heard
    from narrator.normalize import speakable

    return speakable(heard)


def compare(source: str, heard: str) -> tuple[float, list[str]]:
    """Доля слов источника, найденных в распознанном тексте, и список ненайденных."""
    src_words = words(source)
    src = [number_key(w) for w in src_words]
    got = [number_key(w) for w in digits_to_words(words(_spoken(heard)))]
    if not src:
        return 1.0, []
    sm = SequenceMatcher(a=src, b=got, autojunk=False)
    matched: set[int] = set()
    for block in sm.get_matching_blocks():
        matched.update(range(block.a, block.a + block.size))
    missed = [w for i, w in enumerate(src_words) if i not in matched]
    return len(matched) / len(src), missed


def whisper_dir() -> Path:
    return models.locate(models.WHISPER, env="NARRATOR_WHISPER_DIR")


def whisper_source() -> str:
    """Папка модели, а если её нет — имя: тогда модель ищется в кэше HuggingFace."""
    d = whisper_dir()
    return str(d) if (d / "model.bin").exists() else WHISPER_MODEL


class Transcriber:
    def __init__(self):
        # Модель берём только с диска: в сеть не ходим.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        from faster_whisper import WhisperModel

        try:
            self.model = WhisperModel(
                whisper_source(), device="cpu", compute_type="int8", local_files_only=True
            )
        except Exception as e:
            raise RuntimeError(
                f"нет модели faster-whisper {WHISPER_MODEL} в {whisper_dir()} ({e}): "
                f"{models.missing_hint()}"
            ) from e

    def __call__(self, wav: Path) -> str:
        segments, _ = self.model.transcribe(str(wav), language="ru", beam_size=5)
        return " ".join(s.text.strip() for s in segments).strip()


def check(cache_dir: Path, transcribe) -> list[BlockCheck]:
    manifest = json.loads((cache_dir / MANIFEST).read_text(encoding="utf-8"))
    results: list[BlockCheck] = []
    for item in manifest:
        clip = cache_dir / item["clip"]
        asr = clip.with_suffix(".asr.txt")
        if asr.exists():
            heard = asr.read_text(encoding="utf-8")
        else:
            heard = transcribe(clip)
            asr.write_text(heard, encoding="utf-8")
        score, missed = compare(item["text"], heard)
        results.append(BlockCheck(item["line"], item["text"], heard, score, missed))
    return results


def summary(results: list[BlockCheck], threshold: float) -> dict:
    """Отчёт структурой: общий процент и абзацы ниже порога, худшие первыми."""
    total_words = sum(len(words(r.text)) for r in results) or 1
    overall = sum(r.score * len(words(r.text)) for r in results) / total_words
    weak = sorted((r for r in results if r.score < threshold), key=lambda r: r.score)
    return {
        "overall": round(overall, 3),
        "threshold": threshold,
        "total": len(results),
        "weak": [
            {
                "line": r.line,
                "score": round(r.score, 3),
                "text": r.text,
                "heard": r.heard,
                "missed": r.missed,
            }
            for r in weak
        ],
    }


def report(results: list[BlockCheck], threshold: float) -> str:
    total_words = sum(len(words(r.text)) for r in results) or 1
    overall = sum(r.score * len(words(r.text)) for r in results) / total_words
    bad = sorted((r for r in results if r.score < threshold), key=lambda r: r.score)
    lines = [
        "# Проверка озвучки",
        "",
        f"Совпадение по словам: {overall:.0%}, блоков ниже {threshold:.0%}: "
        f"{len(bad)} из {len(results)}.",
        "",
        "Распознавание тоже ошибается: латиница в «услышано» — Whisper узнал термин,",
        "падеж числа может не совпасть. Смотри на слова, которые явно исказились.",
    ]
    for r in bad:
        lines += [
            "",
            f"## Строка {r.line} — {r.score:.0%}",
            "",
            f"- текст: {r.text}",
            f"- услышано: {r.heard}",
            f"- не найдено: {', '.join(r.missed) or '—'}",
        ]
    return "\n".join(lines) + "\n"
