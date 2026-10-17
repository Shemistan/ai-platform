"""Реестр организаций, признанных в РФ экстремистскими, террористическими или нежелательными,
и иностранных агентов (организаций и физлиц).

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

Иностранный агент (status foreign_agent, CMP-012; спека —
docs/superpowers/specs/2026-10-17-foreign-agents-registry.md): упоминание —
сноска, как у запрещённых; материал из его канала (channels записи совпали с
адресом источника дайджеста) — полная маркировка material в начале подписи.
Шаблоны пометок — TEMPLATES, свои — ключ templates в registry.json; тексты
сверяет юрист. Поиск и шаблоны — те же, что у платформы (updates/registry.py).
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
STATUSES = ("extremist", "terrorist", "undesirable", "foreign_agent")
FOREIGN_AGENT = "foreign_agent"
KINDS = ("org", "person")
# Шаблоны пометок иноагента (255-ФЗ ст. 9, 11) — как у платформы; до сверки юристом.
TEMPLATES = {
    "foreign_agent_person": "{name} признан(а) Минюстом РФ иностранным агентом",
    "foreign_agent_org": "Организация {name} признана Минюстом РФ иностранным агентом",
    "foreign_agent_material": (
        "ДАННОЕ СООБЩЕНИЕ (МАТЕРИАЛ) СОЗДАНО И (ИЛИ) РАСПРОСТРАНЕНО ИНОСТРАННЫМ АГЕНТОМ {name} "
        "ЛИБО КАСАЕТСЯ ДЕЯТЕЛЬНОСТИ ИНОСТРАННОГО АГЕНТА {name}"
    ),
}
_SHORT = 4
CAPTION_LIMIT = 1024
# Неудаляемый хвост подписи: сноски («* Компания … признана …») и строки об источнике.
_TAIL_LINE = re.compile(r"^(?:\*\s|\**(?:Источник|Оригинал|Автор|ИИ-контент)\b)")
AI_PREFIX = "ИИ-контент"  # строка об ИИ в хвосте подписи (CMP-011), как у платформы
_SENTENCE_END = re.compile(r"[.!?…»)](?=\s)|\n")
_SPACE = re.compile(r"\s+")
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_HANDLE_HOSTS = ("t.me", "youtube.com", "m.youtube.com")


class CaptionError(ValueError):
    """Сноски и источник не помещаются в подпись."""


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    aliases: tuple[str, ...]
    disclaimer: str
    stopwords: tuple[str, ...] = ()
    status: str = "extremist"
    channels: tuple[str, ...] = ()  # каналы иноагента: адреса и @-имена
    material: str = ""  # маркировка материала иноагента

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


def _clean(value: object) -> str:
    return " ".join(str(value or "").split())


def templates(obj: object) -> dict[str, str]:
    """Шаблоны пометок иноагента: свои из {templates: …} поверх TEMPLATES."""
    own = obj.get("templates") if isinstance(obj, dict) else None
    out = dict(TEMPLATES)
    for key, value in own.items() if isinstance(own, dict) else ():
        if key in TEMPLATES and isinstance(value, str) and "{name}" in value:
            out[key] = _clean(value)
    return out


def parse(obj: object) -> list[Entry]:
    """Записи из JSON ({entries: [...]} или список). Битая запись — ValueError."""
    texts = templates(obj)
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
        name = _clean(item.get("name"))
        status = item["status"]
        disclaimer = _clean(item.get("disclaimer")).rstrip(".")
        material = ""
        if status == FOREIGN_AGENT:
            kind = item.get("kind") if item.get("kind") in KINDS else "org"
            disclaimer = disclaimer or texts[f"foreign_agent_{kind}"].format(name=name)
            material = _clean(item.get("material")).rstrip(".")
            material = material or texts["foreign_agent_material"].format(name=name)
        if not aliases or not disclaimer:
            raise ValueError(f"у записи {item.get('id') or '?'} нет псевдонимов или пометки")
        stopwords = tuple(
            " ".join(str(w).split()) for w in item.get("stopwords") or [] if str(w).strip("* ")
        )
        channels = tuple(_clean(c) for c in item.get("channels") or [] if _clean(c))
        out.append(
            Entry(
                str(item.get("id") or ""),
                name,
                aliases,
                disclaimer,
                stopwords,
                status=status,
                channels=channels,
                material=material,
            )
        )
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


def checked(path: Path | None = None) -> str:
    """Дата последней сверки реестра (версия для журнала комплаенса); нет — пусто."""
    chosen = path or (
        Path(os.environ[REGISTRY_ENV]).expanduser() if os.environ.get(REGISTRY_ENV) else None
    )
    for source in (chosen, DEFAULT_FILE):
        if source is None:
            continue
        try:
            data = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return str(data.get("checked") or "")
    return ""


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


def _channel_key(value: str) -> str:
    """Адрес или @-имя канала в сравнимом виде: без схемы, www, @ и слэшей по краям."""
    value = value.strip().lower()
    value = re.sub(r"^[a-z]+://", "", value)
    value = value.removeprefix("www.").strip("/@ ")
    return value


def _handle(key: str) -> str:
    """@-имя канала Telegram или YouTube из адреса («t.me/s/foo», «youtube.com/@foo», «@foo» → foo).

    Обычный сайт имени не даёт: хвост «/blog» совпал бы у всех."""
    if "/" not in key:
        return key.lstrip("@")
    host, _, rest = key.partition("/")
    if host in _HANDLE_HOSTS and rest:
        parts = rest.removeprefix("s/").split("/")
        if parts[0] in ("channel", "c", "user") and len(parts) > 1:  # youtube.com/channel/UC…
            return parts[1]
        return parts[0].lstrip("@")
    return ""


def source_agent(places: list[str], name: str, entries: list[Entry]) -> Entry | None:
    """Иноагент, чей канал — этот источник: places — его адреса и @-имена, name — название.

    Канал из channels записи совпадает с адресом источника целиком или по
    @-имени Telegram и YouTube; название источника сверяется с псевдонимами.
    """
    keys = {_channel_key(p) for p in places if p and p.strip()}
    handles = {_handle(k) for k in keys} - {""}
    for e in entries:
        if e.status != FOREIGN_AGENT:
            continue
        for channel in map(_channel_key, e.channels):
            if channel and (channel in keys or _handle(channel) in handles):
                return e
        if name and e.mentioned_in(name):
            return e
    return None


def materials(ids: list[str], entries: list[Entry]) -> list[str]:
    """Маркировки материалов иноагентов по id записей, без повторов, в порядке реестра."""
    wanted = set(ids)
    return [e.material for e in entries if e.id in wanted and e.material]


def with_materials(post: str, labels: list[str]) -> str:
    """Пост с маркировкой материалов иноагентов первым абзацем (её место — в начале материала)."""
    fresh = [label for label in labels if label not in post]
    if not fresh:
        return post
    head = "\n".join(fresh)
    return f"{head}\n\n{post.lstrip()}" if post.strip() else f"{head}\n"


def ai_line(*, voice: bool = False, images: bool = False) -> str:
    """Строка об ИИ по фактически использованным генераторам; ничего — пусто."""
    parts = [
        t
        for used, t in (
            (voice, "озвучено ИИ-голосом"),
            (images, "иллюстрации сгенерированы нейросетью"),
        )
        if used
    ]
    return f"{AI_PREFIX}: {'; '.join(parts)}." if parts else ""


def with_ai_line(post: str, line: str) -> str:
    """Пост (markdown) со строкой об ИИ в неудаляемом хвосте."""
    if not line or line in post:
        return post
    return f"{post.rstrip()}\n\n{line}\n" if post.strip() else f"{line}\n"


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
