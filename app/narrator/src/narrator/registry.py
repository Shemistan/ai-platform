"""Реестр организаций, признанных в РФ экстремистскими, террористическими или нежелательными.

Упоминание такой организации в посте или озвучке требует пометки: в подписи
к посту — сноской в конце, в кадре видео — мелким текстом внизу. Решение о
том, как реестр попадает в narrator, — ADR 0001 (docs/decisions).

Откуда список: файл из `$NARRATOR_REGISTRY` (платформа передаёт свой
data/registry.json — его правят на вкладке «Обновления»), иначе — копия
списка платформы по умолчанию рядом, registry.json. Формат тот же, что у
платформы: `{"checked": …, "entries": [{id, name, status, aliases,
disclaimer}, …]}`. Испорченный файл из env — откат на копию по умолчанию.

Псевдоним — слово или фраза; `*` в конце слова — любое окончание
(«Фейсбук*» найдёт «Фейсбуке»). Регистр не важен («META», «meta»); короткий
псевдоним без `*` (до 4 букв) не ищется внутри слова с дефисом («мета-анализ»).
`=` в начале — точный регистр: «=Threads» найдёт продукт Meta, но не «threads»
в тексте про потоки. stopwords записи — слова, которые псевдоним с `*` ловить
не должен («Мет*» → «Метой», но не «метод»): слово целиком, с дефисными
частями, сверяется с ними так же, с `*` на конце.
"""

from __future__ import annotations

import functools
import html
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

REGISTRY_ENV = "NARRATOR_REGISTRY"
DEFAULT_FILE = Path(__file__).with_name("registry.json")
STATUSES = ("extremist", "terrorist", "undesirable")
_SHORT = 4
CAPTION_LIMIT = 1024
# Неудаляемый хвост подписи: сноски («* Компания … признана …») и строки об источнике.
_TAIL_LINE = re.compile(r"^(?:\*\s|\**(?:Источник|Оригинал|Автор)\b)")
_SENTENCE_END = re.compile(r"[.!?…»)](?=\s)|\n")
_SPACE = re.compile(r"\s+")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


class CaptionError(ValueError):
    """Сноски и источник не помещаются в подпись."""


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    aliases: tuple[str, ...]
    disclaimer: str
    stopwords: tuple[str, ...] = ()

    def mentioned_in(self, text: str) -> bool:
        return alias_found(text, self.aliases, self.stopwords)


def _body(alias: str) -> str:
    words = alias.split()
    parts = [re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) for w in words]
    return r"[\s -]+".join(parts)


@functools.lru_cache(maxsize=1024)
def alias_pattern(alias: str) -> re.Pattern[str]:
    flags = 0 if alias.startswith("=") else re.IGNORECASE
    alias = alias.lstrip("=")
    body = _body(alias)
    if "*" not in alias and len(alias) <= _SHORT:
        # «Meta» и «META», но не «Мета-анализ».
        return re.compile(rf"(?<![\w-]){body}(?![\w-])", flags)
    return re.compile(rf"(?<!\w){body}(?!\w)", flags)


@functools.lru_cache(maxsize=1024)
def stop_pattern(word: str) -> re.Pattern[str]:
    return re.compile(_body(word), re.IGNORECASE)


def _whole_word(text: str, start: int, end: int) -> str:
    """Найденное с остатком слова и частями через дефис: «мета» в «мета-анализ» → «мета-анализ»."""
    while start > 0 and (text[start - 1].isalnum() or text[start - 1] in "-_"):
        start -= 1
    while end < len(text) and (text[end].isalnum() or text[end] in "-_"):
        end += 1
    return text[start:end]


def alias_found(text: str, aliases: tuple[str, ...], stopwords: tuple[str, ...] = ()) -> bool:
    """Есть ли в тексте хоть один псевдоним, кроме совпадений со стоп-словами."""
    stops = [stop_pattern(w) for w in stopwords]
    for alias in aliases:
        for m in alias_pattern(alias).finditer(text):
            whole = _whole_word(text, m.start(), m.end())
            if not any(s.fullmatch(whole) for s in stops):
                return True
    return False


def parse(obj: object) -> list[Entry]:
    """Записи из JSON ({entries: [...]} или список). Битая запись — ValueError."""
    raw = obj.get("entries") if isinstance(obj, dict) else obj
    if not isinstance(raw, list):
        raise ValueError("нужен список entries")
    out = []
    for item in raw:
        if not isinstance(item, dict) or item.get("status") not in STATUSES:
            raise ValueError(f"запись {item!r:.80}: нет status из {', '.join(STATUSES)}")
        aliases = tuple(
            " ".join(str(a).split()) for a in item.get("aliases") or [] if str(a).strip()
        )
        disclaimer = " ".join(str(item.get("disclaimer") or "").split()).rstrip(".")
        if not aliases or not disclaimer:
            raise ValueError(f"у записи {item.get('id') or '?'} нет псевдонимов или пометки")
        stopwords = tuple(
            " ".join(str(w).split()) for w in item.get("stopwords") or [] if str(w).strip("* ")
        )
        entry_id, name = str(item.get("id") or ""), str(item.get("name") or "")
        out.append(Entry(entry_id, name, aliases, disclaimer, stopwords))
    return out


def load(path: Path | None = None) -> list[Entry]:
    """Реестр: path, иначе $NARRATOR_REGISTRY, иначе копия по умолчанию."""
    chosen = path or (
        Path(os.environ[REGISTRY_ENV]).expanduser() if os.environ.get(REGISTRY_ENV) else None
    )
    for source in (chosen, DEFAULT_FILE):
        if source is None:
            continue
        try:
            return parse(json.loads(source.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            continue
    return []


def mentioned(text: str, entries: list[Entry]) -> list[Entry]:
    """Организации, которые упоминаются в тексте, в порядке реестра."""
    return [e for e in entries if e.mentioned_in(text)]


def notes(text: str, entries: list[Entry]) -> list[str]:
    """Пометки для кадра и подписи: по одной на каждую упомянутую организацию."""
    return [f"{e.disclaimer}." for e in mentioned(text, entries)]


def missing(post: str, text: str, entries: list[Entry]) -> list[str]:
    """Пометки об организациях из текста или поста, которых в посте ещё нет."""
    return [n for n in notes(f"{text}\n{post}", entries) if n.rstrip(".") not in post]


def with_footnotes(post: str, text: str, entries: list[Entry]) -> str:
    """Пост (markdown) со сносками в конце — о тех, кто есть в тексте или посте и ещё не помечен."""
    lines = "\n".join(f"* {n}" for n in missing(post, text, entries))
    if not lines:
        return post
    return f"{post.rstrip()}\n\n{lines}\n" if post.strip() else f"{lines}\n"


def footnotes_html(text: str, entries: list[Entry]) -> str:
    """Сноски для подписи в HTML: пусто, если упоминаний нет."""
    return "\n".join(f"* {html.escape(n, quote=False)}" for n in notes(text, entries))


# --- подпись в лимит Telegram: тело режется, хвост остаётся целиком ---


def split_tail(post: str) -> tuple[str, str]:
    """(тело, хвост): хвост — последние абзацы из сносок и строк «Источник/Оригинал/Автор»."""
    paras = re.split(r"\n\s*\n", post.strip())
    cut = len(paras)
    while cut > 0 and all(
        _TAIL_LINE.match(line.strip()) for line in paras[cut - 1].splitlines() if line.strip()
    ):
        cut -= 1
    return "\n\n".join(paras[:cut]).strip(), "\n\n".join(paras[cut:]).strip()


def fit_caption(post: str, limit: int = CAPTION_LIMIT) -> str:
    """Пост (markdown) → HTML-подпись не длиннее limit видимых знаков.

    Длинное тело режется по концу предложения (не внутри **жирного** и
    [ссылки](url)), хвост со сносками и источником остаётся целиком. Хвост сам
    не влезает — CaptionError: молча терять пометку нельзя.
    """
    from narrator.news.digest import post_html, visible_length

    full = post_html(post)
    if visible_length(full) <= limit:
        return full
    body, tail = split_tail(post)
    tail_html = post_html(tail) if tail else ""
    budget = limit - (visible_length(tail_html) + 2 if tail_html else 0)
    spans = [(m.start(), m.end()) for rx in (_LINK, _BOLD) for m in rx.finditer(body)]
    free = [n for n in range(len(body), 0, -1) if not any(a < n < b for a, b in spans)]
    sentences = {m.end() for m in _SENTENCE_END.finditer(body)}
    words = {m.start() for m in _SPACE.finditer(body)}
    candidates = [(n, "") for n in free if n in sentences]
    candidates += [(n, "…") for n in free if n in words]
    candidates += [(n, "…") for n in free if n <= budget and n not in words]
    for n, mark in candidates:
        head = body[:n].rstrip()
        if head and visible_length(post_html(head) + mark) <= budget:
            head_html = post_html(head) + mark
            return f"{head_html}\n\n{tail_html}" if tail_html else head_html
    raise CaptionError(
        f"подпись не влезает в {limit} знаков: сноски о запрещённых организациях "
        "и источник занимают весь лимит — сократи пост"
    )
