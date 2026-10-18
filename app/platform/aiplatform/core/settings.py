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
import re
import threading
from pathlib import Path

from aiplatform.core import schema
from aiplatform.core.fsutil import atomic_write

FILE = "settings.json"
SCHEMA = 1  # схема settings.json (core/schema.py, ADR 0009); 1 — поле schema, формат как в схеме 0
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
LIMIT_KINDS = ("llm", "image", "render", "video", "audio", "news", "avatar")  # виды процессов в runner.LIMITS
MAX_LIMIT_MIN = 24 * 60
MAX_POLICY_DAYS = 3650  # trim_days / archive_days уборки
MAX_FREE_GB = 10000  # cleanup.min_free_gb
REGISTRY_MAX_AGE_DAYS = 14
SCENARIO_IDS = ("digest", "article", "post")  # как studio.service.SCENARIOS


def _empty() -> dict:
    return {
        "disabled": {**{kind: [] for kind in KINDS}, "voices": {}},
        "schedule": {"digest": {"enabled": False, "time": "07:00", "day": "yesterday"}},
        "notify": {"enabled": False, "telegram_chat": ""},
        "stats": {"auto": True},
        # Уборка (core/cleanup.py): days — задачи вкладок, кэш, архив и загрузки старше; trim_days —
        # через сколько дней после публикации у выпуска удаляются картинки и исходники клипов;
        # archive_days — когда опубликованный выпуск уходит в архив сам (0 — никогда);
        # min_free_gb — порог места до GPU-задач и сборки (None — 10 ГБ, в демо без проверки);
        # draft_days — неопубликованный выпуск без движения дольше уходит в архив сам (0 — никогда).
        "cleanup": {
            "auto": True,
            "days": 30,
            "trim_days": 14,
            "archive_days": 60,
            "draft_days": 7,
            "min_free_gb": None,
        },
        # Каналы по умолчанию по сценариям (PRD-005): id из channels.json; None — не заданы
        # (у дайджеста тогда VOICE_NEWS движка, если такой канал есть).
        "default_channels": {s: None for s in SCENARIO_IDS},
        # Дайджест по умолчанию: до скольких (ЧЧ:ММ) ждать свою обложку, потом рисовать локально;
        # пусто — ждать без срока (PRD-002).
        "digest_defaults": {"poster_deadline": ""},
        # Предложения на доске: false — человек нажал «Не предлагать» (PRD-003).
        "offers": {"digest_autopilot": True},
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


def _number(value: object, top: float) -> bool:
    """Число от 0 до top включительно (bool — не число)."""
    return isinstance(value, int | float) and not isinstance(value, bool) and 0 <= value <= top


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
    out["cleanup"]["auto"] = _flag(cleanup.get("auto"), True)
    if cleanup.get("days") in CLEANUP_DAYS:
        out["cleanup"]["days"] = cleanup["days"]
    for name in ("trim_days", "archive_days", "draft_days"):
        if _number(cleanup.get(name), MAX_POLICY_DAYS) and float(cleanup[name]).is_integer():
            out["cleanup"][name] = int(cleanup[name])
    if _number(cleanup.get("min_free_gb"), MAX_FREE_GB):
        out["cleanup"]["min_free_gb"] = cleanup["min_free_gb"]
    out["offers"]["digest_autopilot"] = _flag(_section(raw, "offers").get("digest_autopilot"), True)
    deadline = _section(raw, "digest_defaults").get("poster_deadline")
    if isinstance(deadline, str) and _TIME.match(deadline):
        out["digest_defaults"]["poster_deadline"] = deadline
    chosen = _section(raw, "default_channels")
    for scenario in SCENARIO_IDS:
        value = chosen.get(scenario)
        if isinstance(value, list):
            out["default_channels"][scenario] = list(dict.fromkeys(v for v in value if isinstance(v, str) and v))
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
        self.problem: str | None = None  # файл записан более новой версией — читаем, не пишем (BE-008)
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
        self.problem = None
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            schema.upgrade(raw if isinstance(raw, dict) else {}, SCHEMA, where=FILE)
        except schema.TooNew as exc:  # читаем как есть, но не пишем: правки — в той, новой версии
            self.problem = f"Настройки: {exc}."
        except (OSError, ValueError):
            raw = None
        data = normalize(raw)
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
            if self.problem:
                raise schema.TooNew(self.problem)
            apply(data)
            data = normalize(data)
            text = json.dumps(schema.stamp(data, SCHEMA), ensure_ascii=False, indent=2)
            atomic_write(self.path, text + "\n")
            self._cache = None
            return data
