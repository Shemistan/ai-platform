"""Реестр организаций, признанных в РФ экстремистскими, террористическими или нежелательными.

Если такая организация упоминается в выпуске, на кадре мелким текстом идёт
стандартная пометка, а в подпись к посту она дописывается в конце. Список
по умолчанию — registry.json рядом (IT-компании и группировки, которые
могут попасть в новости); правки и обновления с вкладки «Обновления»
ложатся в data/registry.json и действуют со следующей сборки видео.

Псевдоним — слово или фраза; `*` в конце слова — любое окончание
(«Фейсбук*» найдёт «Фейсбуке»). Короткие псевдонимы без `*` (до 4 букв,
например «Meta») ищутся с учётом регистра, чтобы не ловить обычные слова.
"""

from __future__ import annotations

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
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_CASE_SENSITIVE_MAX = 4


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    status: str
    aliases: tuple[str, ...]
    disclaimer: str
    basis: str = ""
    source: str = ""

    def public(self) -> dict:
        return asdict(self) | {"aliases": list(self.aliases), "status_label": STATUSES[self.status]}

    def mentioned_in(self, text: str) -> bool:
        return any(alias_pattern(a).search(text) for a in self.aliases)


def alias_pattern(alias: str) -> re.Pattern[str]:
    words = alias.split()
    parts = [re.escape(w[:-1]) + r"\w*" if w.endswith("*") else re.escape(w) for w in words]
    body = r"[\s -]+".join(parts)
    if "*" not in alias and len(alias) <= _CASE_SENSITIVE_MAX:
        # «Meta», но не «мета» и не «Мета-анализ».
        return re.compile(rf"(?<![\w-]){body}(?![\w-])")
    return re.compile(rf"(?<!\w){body}(?!\w)", re.IGNORECASE)


def _text(item: dict, key: str, limit: int, *, required: bool = True) -> str:
    value = " ".join(str(item.get(key) or "").split())
    if required and not value:
        raise ValueError(f"у записи {item.get('id') or item.get('name') or '?'} пусто поле {key}")
    return value[:limit]


def _web(url: str) -> str:
    """Источник — только http(s)-ссылка: вкладка «Обновления» ставит его в href."""
    parts = urlsplit(url)
    return url if parts.scheme.lower() in ("http", "https") and parts.netloc else ""


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
        names = tuple(dict.fromkeys(" ".join(str(a).split()) for a in aliases if str(a or "").strip()))
        if not names or any(a.strip("*") == "" for a in names):
            raise ValueError(f"у записи {entry_id} нет псевдонимов для поиска")
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
            "entries": [asdict(e) | {"aliases": list(e.aliases)} for e in entries],
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)

    def reset(self) -> None:
        with self._lock:
            self.path.unlink(missing_ok=True)
