"""Реестр организаций, признанных в РФ экстремистскими, террористическими или нежелательными.

Если такая организация упоминается в выпуске, на кадре мелким текстом идёт
стандартная пометка, а в подпись к посту она дописывается в конце. Список
по умолчанию — registry.json рядом (IT-компании и группировки, которые
могут попасть в новости); правки и обновления с вкладки «Обновления»
ложатся в data/registry.json и действуют со следующей сборки видео.

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
import json
import re
import threading
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_FILE = Path(__file__).with_name("registry.json")
FILE_NAME = "registry.json"  # свой реестр платформы — в корне data: его читают сценарии
STATUSES = {
    "extremist": "экстремистская",
    "terrorist": "террористическая",
    "undesirable": "нежелательная",
}
MAX_ENTRIES = 200
MAX_STOPWORDS = 80
MAX_AGE_DAYS = 14  # порог свежести по умолчанию; свой — settings.json, registry.max_age_days
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHORT = 4


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    status: str
    aliases: tuple[str, ...]
    disclaimer: str
    basis: str = ""
    source: str = ""
    stopwords: tuple[str, ...] = ()
    ref: str = ""  # номер в официальном перечне
    archive_url: str = ""  # снимок первоисточника (web.archive.org и т. п.)

    @property
    def confirmed(self) -> bool:
        """Основание проверяемо: есть номер в перечне и снимок первоисточника."""
        return bool(self.ref and self.archive_url)

    def public(self) -> dict:
        return self.stored() | {"status_label": STATUSES[self.status], "confirmed": self.confirmed}

    def stored(self) -> dict:
        """Запись для JSON: кортежи — списками."""
        return asdict(self) | {"aliases": list(self.aliases), "stopwords": list(self.stopwords)}

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


def _text(item: dict, key: str, limit: int, *, required: bool = True) -> str:
    value = " ".join(str(item.get(key) or "").split())
    if required and not value:
        raise ValueError(f"у записи {item.get('id') or item.get('name') or '?'} пусто поле {key}")
    return value[:limit]


def _web(url: str) -> str:
    """Источник — только http(s)-ссылка: вкладка «Обновления» ставит его в href."""
    parts = urlsplit(url)
    return url if parts.scheme.lower() in ("http", "https") and parts.netloc else ""


def _words(items: list) -> tuple[str, ...]:
    return tuple(dict.fromkeys(" ".join(str(a).split()) for a in items if str(a or "").strip()))


def parse(obj: object) -> list[Entry]:
    """Записи из JSON ({entries: [...]} или список). Ошибка в записи — ValueError с её именем."""
    raw = obj.get("entries") if isinstance(obj, dict) else obj
    if not isinstance(raw, list):
        raise ValueError("нужен список entries")
    out: list[Entry] = []
    seen: set[str] = set()
    for item in raw[:MAX_ENTRIES]:
        if not isinstance(item, dict):
            raise ValueError("каждая запись — объект")
        entry_id = str(item.get("id") or "").strip().lower()
        if not _ID.match(entry_id):
            raise ValueError(f"id «{entry_id}» — латиница, цифры, - и _")
        if entry_id in seen:
            raise ValueError(f"id «{entry_id}» повторяется")
        status = item.get("status")
        if status not in STATUSES:
            raise ValueError(f"у записи {entry_id} status — одно из: {', '.join(STATUSES)}")
        aliases = item.get("aliases")
        if not isinstance(aliases, list):
            raise ValueError(f"у записи {entry_id} aliases — список")
        names = _words(aliases)
        if not names or any(a.strip("=*") == "" for a in names):
            raise ValueError(f"у записи {entry_id} нет псевдонимов для поиска")
        stopwords = item.get("stopwords") or []
        if not isinstance(stopwords, list):
            raise ValueError(f"у записи {entry_id} stopwords — список")
        seen.add(entry_id)
        out.append(
            Entry(
                id=entry_id,
                name=_text(item, "name", 120),
                status=status,
                aliases=names[:30],
                disclaimer=_text(item, "disclaimer", 400).rstrip("."),
                basis=_text(item, "basis", 400, required=False),
                source=_web(_text(item, "source", 400, required=False)),
                stopwords=tuple(w for w in _words(stopwords) if w.strip("*"))[:MAX_STOPWORDS],
                ref=_text(item, "ref", 60, required=False),
                archive_url=_web(_text(item, "archive_url", 400, required=False)),
            )
        )
    return out


def mentioned(text: str, entries: list[Entry]) -> list[Entry]:
    """Организации, которые упоминаются в тексте, в порядке реестра."""
    return [e for e in entries if e.mentioned_in(text)]


def notes(text: str, entries: list[Entry]) -> list[str]:
    """Пометки для кадра: по одной на каждую упомянутую организацию."""
    return [f"{e.disclaimer}." for e in mentioned(text, entries)]


def with_footnotes(post: str, text: str, entries: list[Entry]) -> str:
    """Подпись к посту с пометками в конце — о тех, кто есть в тексте или подписи и ещё не помечен."""
    missing = [n for n in notes(f"{text}\n{post}", entries) if n.rstrip(".") not in post]
    if not missing:
        return post
    lines = "\n".join(f"* {n}" for n in missing)
    return f"{post.rstrip()}\n\n{lines}\n" if post.strip() else f"{lines}\n"


def age_days(checked: str, today: date | None = None) -> int | None:
    """Сколько дней назад сверяли реестр; None — дата не задана или битая."""
    try:
        return ((today or date.today()) - date.fromisoformat(checked)).days
    except (TypeError, ValueError):
        return None


def stale_note(checked: str, max_days: int = MAX_AGE_DAYS, today: date | None = None) -> str:
    """Предупреждение о несверенном реестре (пусто — свежий): вкладка «Обновления» и публикация."""
    age = age_days(checked, today)
    if age is None:
        return "Реестр запрещённых организаций не сверяли — запусти сверку на вкладке «Обновления»."
    if age <= max_days:
        return ""
    return (
        f"Реестр запрещённых организаций сверяли {age} дн. назад ({checked}), порог — {max_days} дн.: "
        "сверь его на вкладке «Обновления», иначе пометки могут быть неполными."
    )


def diff(old: list[Entry], new: list[Entry]) -> dict:
    """Чем новый список отличается от текущего: id добавленных, убранных и изменённых записей."""
    before, after = {e.id: e for e in old}, {e.id: e for e in new}
    return {
        "added": [i for i in after if i not in before],
        "removed": [i for i in before if i not in after],
        "changed": [i for i in after if i in before and after[i] != before[i]],
    }


class Registry:
    """Реестр на диске: свой файл в data, пока его нет — список по умолчанию."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def read(self) -> tuple[list[Entry], str]:
        """(записи, дата последней сверки). Испорченный свой файл — откат на список по умолчанию."""
        for source in (self.path, DEFAULT_FILE):
            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                return parse(data), str(data.get("checked") or "") if isinstance(data, dict) else ""
            except (OSError, ValueError):
                continue
        return [], ""

    def entries(self) -> list[Entry]:
        return self.read()[0]

    def customized(self) -> bool:
        return self.path.is_file()

    def save(self, entries: list[Entry], checked: str | None = None) -> None:
        data = {
            "checked": checked or date.today().isoformat(),
            "entries": [e.stored() for e in entries],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)

    def reset(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
