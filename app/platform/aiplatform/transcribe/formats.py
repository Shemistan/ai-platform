"""Слова расшифровки → текст абзацами, Markdown с таймкодами, субтитры SRT/VTT.

Слово — {"w": текст с пунктуацией, "s": начало, "e": конец} в секундах, как у воркера «Монтажа».
"""

from __future__ import annotations

from aiplatform.montage import render

PARAGRAPH_GAP_S = 1.2  # пауза после конца фразы — новый абзац
HARD_GAP_S = 2.5  # пауза такой длины рвёт абзац и посреди фразы
MAX_PARAGRAPH_WORDS = 120  # длинный монолог без пауз — режем по концу фразы
SUB_WORDS = 10
SUB_S = 5.0
_END = ".!?…"


def paragraphs(words: list[dict]) -> list[dict]:
    """[{"text", "s", "e"}]: абзац рвётся на длинной паузе после конца фразы или на очень длинной паузе."""
    out: list[dict] = []
    current: list[dict] = []
    for word in words:
        if current:
            gap = word["s"] - current[-1]["e"]
            sentence_end = current[-1]["w"][-1:] in _END
            if gap >= HARD_GAP_S or (sentence_end and (gap >= PARAGRAPH_GAP_S or len(current) >= MAX_PARAGRAPH_WORDS)):
                out.append(_join(current))
                current = []
        current.append(word)
    if current:
        out.append(_join(current))
    return out


def _join(words: list[dict]) -> dict:
    return {"text": " ".join(w["w"].strip() for w in words), "s": words[0]["s"], "e": words[-1]["e"]}


def clock(t: float) -> str:
    """Секунды → `м:сс` или `ч:мм:сс`."""
    total = int(t)
    h, m, s = total // 3600, total // 60 % 60, total % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def txt(words: list[dict]) -> str:
    return "\n\n".join(p["text"] for p in paragraphs(words)) + "\n"


def md(words: list[dict], title: str) -> str:
    body = "\n\n".join(f"**[{clock(p['s'])}]** {p['text']}" for p in paragraphs(words))
    return f"# {title}\n\n{body}\n"


def cues(words: list[dict]) -> list[dict]:
    return render.chunks(words, max_words=SUB_WORDS, max_s=SUB_S)


def srt(words: list[dict]) -> str:
    return render.srt(cues(words))


def vtt(words: list[dict]) -> str:
    return render.vtt(cues(words))
