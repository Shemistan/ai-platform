"""Версия схемы файлов данных, миграции при чтении и карантин битых записей (BE-008, ADR 0009).

Каждый файл данных несёт поле `schema: N`. Файл без него — схема 0 (до BE-008).
Код знает свою схему (CURRENT у каждого вида файла) и при чтении поднимает
старый файл цепочкой migrations[v]: v → v+1. Файл новее кода не трогаем:
читатель получает TooNew и показывает «записано более новой версией».
Списки (published.json, channels.json) со схемы 1 лежат в обёртке
`{"schema": 1, "<поле>": [...]}`; голый список читается как схема 0.

Запись (выпуск, задача), которая не JSON или без обязательных полей, уходит
в карантин: файл переименовывается в `*.bad`, причина — в problems() вкладки,
остальные записи грузятся как обычно.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from aiplatform.core import log
from aiplatform.core.fsutil import atomic_write_json

KEY = "schema"
Migration = Callable[[Any], Any]
Check = Callable[[dict], str | None]


class TooNew(ValueError):  # noqa: N818 — состояние файла, а не сбой
    """Файл записан более новой версией кода: читать можно на свой страх, писать — нельзя."""


class Broken(ValueError):  # noqa: N818
    """Файл не JSON или в нём нет обязательных полей."""


def version_of(data: object) -> int:
    if isinstance(data, dict):
        value = data.get(KEY, 0)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
        raise Broken(f"поле {KEY} — не целое число ≥ 0: {value!r}")
    return 0


def upgrade(data: Any, current: int, migrations: Mapping[int, Migration] | None = None, *, where: str = "") -> Any:
    """Поднять данные до схемы current. Нет шага миграции — формат не менялся, только номер."""
    version = version_of(data)
    if version > current:
        raise TooNew(f"{where}: записано более новой версией (схема {version}, эта версия знает {current}) — не тронут")
    while version < current:
        step = (migrations or {}).get(version)
        if step is not None:
            data = step(data)
        version += 1
    if isinstance(data, dict):
        data[KEY] = current
    return data


def stamp(data: dict, version: int) -> dict:
    """Копия для записи: schema — первым полем, чтобы его было видно в начале файла."""
    return {KEY: version, **{k: v for k, v in data.items() if k != KEY}}


def wrap(items: list, field: str, version: int) -> dict:
    return {KEY: version, field: items}


def unwrap(data: object, field: str) -> list:
    """Список из обёртки {"schema", field: [...]} или голый список схемы 0."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict) and isinstance(data.get(field), list):
        return data[field]
    raise Broken(f"ожидается список или объект с полем {field}")


def quarantine(path: Path) -> Path:
    """Переименовать битый файл в *.bad (с номером, если такой уже есть)."""
    target = path.with_name(path.name + ".bad")
    n = 1
    while target.exists():
        n += 1
        target = path.with_name(f"{path.name}.{n}.bad")
    path.replace(target)
    return target


def require(*fields: str, types: Mapping[str, type | tuple[type, ...]] | None = None) -> Check:
    """Проверка записи: обязательные поля и их типы; None — запись годна."""

    def check(data: dict) -> str | None:
        missing = [f for f in fields if f not in data]
        if missing:
            return "нет полей " + ", ".join(missing)
        for name, kind in (types or {}).items():
            if not isinstance(data.get(name), kind):
                return f"поле {name} неверного типа"
        return None

    return check


def load_record(
    path: Path,
    current: int,
    *,
    check: Check | None = None,
    migrations: Mapping[int, Migration] | None = None,
    problems: list[str] | None = None,
    label: str = "",
) -> tuple[dict | None, bool]:
    """Прочитать запись: (данные, поднята ли схема) или (None, False) — пропустить.

    Битая запись — в карантин и в problems; запись новее кода — только в problems.
    Не прочитался файл (OSError) — пропуск без карантина: сбой может быть временным.
    """
    label = label or str(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, False
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise Broken("не объект JSON")
        before = version_of(data)
        data = upgrade(data, current, migrations, where=label)
        reason = check(data) if check else None
        if reason:
            raise Broken(reason)
    except TooNew as exc:
        _report(problems, str(exc))
        return None, False
    except (json.JSONDecodeError, UnicodeDecodeError, Broken, KeyError, TypeError) as exc:
        try:
            bad = quarantine(path)
        except OSError:
            bad = path
        _report(problems, f"{label}: битая запись ({exc}) — отложена в {bad.name}")
        return None, False
    return data, before != current


def _report(problems: list[str] | None, text: str) -> None:
    log.event("data.quarantine" if "битая" in text else "data.too_new", level=logging.WARNING, error=text)
    if problems is not None:
        problems.append(text)


# Схема job.json вкладок «Фото», «Видео», «Аудио»: 1 — поле schema, формат как в схеме 0.
JOB_SCHEMA = 1
JOB_FIELDS = require("id", "status", "created_at", "progress", types={"id": str, "status": str, "progress": dict})


# --- откат кода (ADR 0009) ---
# Код до BE-008 читает published.json и channels.json только голым списком. Перед откатом
# на такую версию обёртки разворачиваем обратно: `python -m aiplatform.core.schema unwrap <data>`.
LIST_FILES = (("studio/projects/*/published.json", "records"), ("channels.json", "channels"))


def unwrap_lists(data_dir: Path) -> list[Path]:
    """Обёртки {"schema", поле: [...]} → голые списки (схема 0); возвращает изменённые файлы."""
    changed = []
    for pattern, field in LIST_FILES:
        for path in sorted(data_dir.glob(pattern)):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and isinstance(data.get(field), list):
                atomic_write_json(path, data[field])
                changed.append(path)
    return changed


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] != "unwrap":
        print("usage: python -m aiplatform.core.schema unwrap <каталог данных>", file=sys.stderr)
        return 2
    for path in unwrap_lists(Path(args[1]).expanduser()):
        print(f"развёрнут в список: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
