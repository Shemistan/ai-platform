"""Права на файлы с секретами и личными данными: только владелец (SEC-010).

Токен бота, каналы, подписчики и каталог данных не должны читаться другими
пользователями машины. Запись — атомарно через временный файл 0600;
проверка — для баннера проблем платформы.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

PRIVATE_FILE = 0o600
PRIVATE_DIR = 0o700


def write_private(path: Path, text: str) -> None:
    """Атомарно записать текст в файл с правами 0600 (mkstemp создаёт временный уже таким)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def loose(path: Path) -> bool:
    """Есть ли у файла/каталога права для группы или всех. Нет файла — не проблема."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IRWXG | stat.S_IRWXO))


def warning(path: Path, what: str) -> str | None:
    """Текст для problems(), если `path` доступен не только владельцу."""
    if not loose(path):
        return None
    mode = "700" if path.is_dir() else "600"
    return f"{what} {path} доступен другим пользователям машины — chmod {mode} '{path}'."
