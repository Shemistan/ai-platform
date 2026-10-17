"""Общие настройки платформы: <data>/settings.json, правятся во вкладке «Настройки».

В них: что выключено (модели фото и видео, движки озвучки и отдельные
голоса — выключенное пропадает из выбора во всех вкладках и сценариях и не
принимается в запросах), расписание дайджеста, уведомления (и чат владельца
в Telegram), обновление отклика каналов, уборка старых данных и лимиты
времени процессов (`timeouts`, только руками в файле). Файл читается при каждом обращении
(с кэшем по mtime), поэтому ручная правка подхватывается без перезапуска.
"""

from __future__ import annotations

import json
import os
import re
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


DIGEST_DAYS = ("yesterday", "today")  # дайджест по расписанию — за вчера или за сегодня
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CLEANUP_DAYS = (7, 14, 30, 60, 90)
_CHAT = re.compile(r"-?\d+|@\w+")  # личный чат владельца: id или @username
LIMIT_KINDS = ("llm", "image", "render", "video", "audio", "news")  # виды процессов в runner.LIMITS
MAX_LIMIT_MIN = 24 * 60
REGISTRY_MAX_AGE_DAYS = 14


def _empty() -> dict:
    return {
        "disabled": {**{kind: [] for kind in KINDS}, "voices": {}},
        "schedule": {"digest": {"enabled": False, "time": "07:00", "day": "yesterday"}},
        "notify": {"enabled": False, "telegram_chat": ""},
        "stats": {"auto": True},
        "cleanup": {"auto": False, "days": 30},
        # Реестр запрещённых организаций: ежедневная сверка в фоне и порог свежести, дни (CMP-013).
        "registry": {"auto_check": True, "max_age_days": REGISTRY_MAX_AGE_DAYS},
        # Лимиты времени процессов по виду (runner.LIMITS), минуты: {"llm": {"total": 10, "idle": 5}}.
        "timeouts": {},
    }


def _timeouts(raw: object) -> dict:
    out: dict = {}
    for kind, value in raw.items() if isinstance(raw, dict) else ():
        if kind not in LIMIT_KINDS or not isinstance(value, dict):
            continue
        limits = {
            name: value[name]
            for name in ("total", "idle")
            if isinstance(value.get(name), int | float)
            and not isinstance(value.get(name), bool)
            and 0 < value[name] <= MAX_LIMIT_MIN
        }
        if limits:
            out[kind] = limits
    return out


def _flag(value: object, default: bool) -> bool:
    return value if isinstance(value, bool) else default


def _section(raw: object, name: str) -> dict:
    value = raw.get(name) if isinstance(raw, dict) else None
    return value if isinstance(value, dict) else {}


def _strings(value: object) -> list[str]:
    return sorted({v for v in value if isinstance(v, str)}) if isinstance(value, list) else []


def normalize(raw: object) -> dict:
    """Чужие ключи и мусор отбрасываются: битый файл не роняет платформу."""
    out = _empty()
    digest = _section(_section(raw, "schedule"), "digest")
    plan = out["schedule"]["digest"]
    plan["enabled"] = _flag(digest.get("enabled"), False)
    if isinstance(digest.get("time"), str) and _TIME.match(digest["time"]):
        plan["time"] = digest["time"]
    if digest.get("day") in DIGEST_DAYS:
        plan["day"] = digest["day"]
    notify = _section(raw, "notify")
    chat = notify.get("telegram_chat")
    out["notify"]["telegram_chat"] = chat.strip() if isinstance(chat, str) and _CHAT.fullmatch(chat.strip()) else ""
    # Чат владельца задан — уведомления включены, пока человек явно не выключил.
    out["notify"]["enabled"] = _flag(notify.get("enabled"), bool(out["notify"]["telegram_chat"]))
    out["timeouts"] = _timeouts(raw.get("timeouts") if isinstance(raw, dict) else None)
    out["stats"]["auto"] = _flag(_section(raw, "stats").get("auto"), True)
    cleanup = _section(raw, "cleanup")
    out["cleanup"]["auto"] = _flag(cleanup.get("auto"), False)
    if cleanup.get("days") in CLEANUP_DAYS:
        out["cleanup"]["days"] = cleanup["days"]
    reg = _section(raw, "registry")
    out["registry"]["auto_check"] = _flag(reg.get("auto_check"), True)
    age = reg.get("max_age_days")
    if isinstance(age, int) and not isinstance(age, bool) and 1 <= age <= 365:
        out["registry"]["max_age_days"] = age
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
