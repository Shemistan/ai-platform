"""Атомарная запись файлов: tmp рядом с целью и os.replace.

Имя tmp уникально (`.{name}.{uuid4}.tmp`): два писателя одного файла не
портят tmp друг друга, а читатель видит либо старый файл целиком, либо
новый. Крах посреди записи оставляет только «.tmp», цель цела.
"""

from __future__ import annotations

import contextlib
import json
import os
import uuid
from pathlib import Path


def tmp_path(path: Path) -> Path:
    """Уникальный tmp в каталоге цели: os.replace атомарен только в пределах одной ФС."""
    return path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")


def atomic_write(path: Path, data: bytes | str, *, mode: int | None = None) -> None:
    """Записать data в path целиком или не записать вовсе. mode — права файла (например, 0o600)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tmp_path(path)
    raw = data.encode("utf-8") if isinstance(data, str) else data
    try:
        with open(tmp, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def atomic_write_json(path: Path, data: object) -> None:
    """JSON с отступами и переводом строки в конце — как пишут файлы данных платформы."""
    atomic_write(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")
