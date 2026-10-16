"""Кэш генераций сценариев: картинка, ролик или озвучка с теми же входами второй раз не делаются.

Запись — `<data>/studio/cache/<id><расширение>`, id — sha256 от JSON входов
(вид, промпт, модель, размер…). Файл в проект кладётся клоном APFS
(copy-on-write: места не прибавляется, правка одной копии не трогает
другую), а где клонов нет — обычной копией. При попадании mtime записи
обновляется — уборка удаляет давно не нужные записи.
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path


def _clonefile():
    if sys.platform != "darwin":
        return None
    try:
        fn = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True).clonefile
    except (OSError, AttributeError):
        return None
    fn.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32)
    return fn


_CLONE = _clonefile()


def entry_id(kind: str, **inputs: object) -> str:
    raw = json.dumps({"kind": kind, **inputs}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def place(source: Path, target: Path) -> None:
    """Положить копию файла (клоном APFS, если можно); target заменяется атомарно."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    tmp.unlink(missing_ok=True)
    if _CLONE is None or _CLONE(os.fsencode(source), os.fsencode(tmp), 0) != 0:
        shutil.copyfile(source, tmp)
    tmp.replace(target)


class MediaCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def find(self, entry: str) -> Path | None:
        if not self.root.is_dir():
            return None
        found = sorted(p for p in self.root.glob(f"{entry}.*") if not p.name.endswith(".tmp"))
        return found[0] if found else None

    def take(self, entry: str, target: Path) -> bool:
        """Есть запись — положить её в target и вернуть True."""
        source = self.find(entry)
        if source is None:
            return False
        place(source, target)
        now = time.time()
        with contextlib.suppress(OSError):
            os.utime(source, (now, now))
        return True

    def put(self, entry: str, source: Path) -> None:
        """Сохранить готовый файл в кэш. Сбой кэша не мешает выпуску."""
        with contextlib.suppress(OSError):
            self.root.mkdir(parents=True, exist_ok=True)
            for old in self.root.glob(f"{entry}.*"):
                old.unlink(missing_ok=True)
            place(source, self.root / f"{entry}{source.suffix.lower()}")
