"""Длинная статья → серия коротких выпусков, без LLM.

Части режутся по заголовкам (`#`, `##` в начале строки). Мелкие главы
склеиваются с соседними, крупные режутся по абзацам — каждая часть около
PART_WORDS слов (~8 минут озвучки). Без заголовков — просто группы абзацев.
Текст части сохраняет заголовки: слайды и полоса главы их подхватят.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PART_WORDS = 1000
_HEADING = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*#*\s*$")


@dataclass(frozen=True)
class Part:
    title: str  # первая глава части; пусто — без заголовка
    text: str

    @property
    def words(self) -> int:
        return len(self.text.split())


def _blocks(text: str) -> list[str]:
    """Абзацы как в тексте — куски между пустыми строками, с исходными строками."""
    out, buf = [], []
    for line in text.splitlines() + [""]:
        if line.strip():
            buf.append(line.rstrip())
        elif buf:
            out.append("\n".join(buf))
            buf = []
    return out


def _heading(block: str) -> str | None:
    m = _HEADING.match(block.splitlines()[0])
    return m[1].strip() if m else None


def chapters(text: str) -> list[Part]:
    """Главы по заголовкам; текст до первого заголовка — своя глава без названия."""
    out: list[tuple[str, list[str]]] = []
    for block in _blocks(text):
        name = _heading(block)
        if name is not None or not out:
            out.append((name or "", []))
        out[-1][1].append(block)
    return [Part(title, "\n\n".join(blocks)) for title, blocks in out if blocks]


def _by_paragraphs(part: Part, limit: int) -> list[Part]:
    """Глава длиннее лимита — куски по абзацам, примерно поровну."""
    blocks = _blocks(part.text)
    total = sum(len(b.split()) for b in blocks)
    pieces = max(1, round(total / limit))
    target = total / pieces
    out: list[list[str]] = [[]]
    count = 0
    for block in blocks:
        if out[-1] and count + len(block.split()) / 2 > target and len(out) < pieces:
            out.append([])
            count = 0
        out[-1].append(block)
        count += len(block.split())
    return [Part(part.title, "\n\n".join(chunk)) for chunk in out if chunk]


def split(text: str, limit: int = PART_WORDS) -> list[Part]:
    """Части серии по ~limit слов: главы целиком, где это возможно."""
    pieces: list[Part] = []
    for chapter in chapters(text):
        pieces.extend(_by_paragraphs(chapter, limit) if chapter.words > limit * 1.5 else [chapter])
    out: list[Part] = []
    for piece in pieces:
        if out and out[-1].words + piece.words <= limit:
            last = out[-1]
            out[-1] = Part(last.title or piece.title, f"{last.text}\n\n{piece.text}")
        else:
            out.append(piece)
    # Хвост меньше трети части — к предыдущей: короткий выпуск в конце серии не нужен.
    if len(out) > 1 and out[-1].words < limit / 3:
        tail = out.pop()
        out[-1] = Part(out[-1].title, f"{out[-1].text}\n\n{tail.text}")
    return out
