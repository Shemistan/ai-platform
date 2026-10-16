"""Общие настройки платформы: <data>/settings.json, правятся во вкладке «Настройки».

Пока в них одно — что выключено: модели фото и видео, движки озвучки и
отдельные голоса. Выключенное пропадает из выбора во всех вкладках и
сценариях и не принимается в запросах. Файл читается при каждом обращении
(с кэшем по mtime), поэтому ручная правка подхватывается без перезапуска.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

FILE = "settings.json"
KINDS = ("photo", "video", "audio")  # модели фото, модели видео, движки озвучки
_STORES: dict[Path, Settings] = {}
_STORES_LOCK = threading.Lock()


def at(data_dir: Path) -> Settings:
    """Один объект на файл: запись и чтение из разных вкладок идут под одним замком."""
    path = (data_dir / FILE).resolve()
    with _STORES_LOCK:
        store = _STORES.get(path)
        if store is None:
            store = _STORES[path] = Settings(path)
        return store


def _empty() -> dict:
    return {"disabled": {**{kind: [] for kind in KINDS}, "voices": {}}}


def _strings(value: object) -> list[str]:
    return sorted({v for v in value if isinstance(v, str)}) if isinstance(value, list) else []


def normalize(raw: object) -> dict:
    """Чужие ключи и мусор отбрасываются: битый файл не роняет платформу."""
    out = _empty()
    disabled = raw.get("disabled") if isinstance(raw, dict) else None
    if not isinstance(disabled, dict):
        return out
    for kind in KINDS:
        out["disabled"][kind] = _strings(disabled.get(kind))
    voices = disabled.get("voices")
    if isinstance(voices, dict):
        for engine, names in voices.items():
            names = _strings(names)
            if isinstance(engine, str) and names:
                out["disabled"]["voices"][engine] = names
    return out


class Settings:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._cache: tuple[tuple[int, int], dict] | None = None

    def load(self) -> dict:
        with self._lock:
            return self._read()

    def _read(self) -> dict:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return _empty()
        mtime = (stat.st_mtime_ns, stat.st_size)
        if self._cache and self._cache[0] == mtime:
            return json.loads(json.dumps(self._cache[1]))
        try:
            data = normalize(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            data = _empty()
        self._cache = (mtime, data)
        return json.loads(json.dumps(data))

    def disabled(self, kind: str) -> set[str]:
        return set(self.load()["disabled"][kind])

    def disabled_voices(self) -> dict[str, set[str]]:
        return {engine: set(names) for engine, names in self.load()["disabled"]["voices"].items()}

    def change(self, apply) -> dict:
        """apply(data) правит копию настроек; результат пишется атомарно и возвращается."""
        with self._lock:
            data = self._read()
            apply(data)
            data = normalize(data)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
            self._cache = None
            return data
