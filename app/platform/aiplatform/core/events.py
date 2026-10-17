"""Журнал событий: append-only `<data>/events.jsonl` для аналитики выпусков (AN-002).

Строка — событие JSON. Обязательные поля: event_id, ts, event, actor; остальные —
по схеме аудита (FIELDS), если известны. Неизвестные поля уходят в props.
Состояние шага в project.json перезаписывается, а журнал — нет: по нему
считаются время на ручные шаги, перезапуски, доля падений и воронка выпуска.

actor: system — шаг выпуска, human — действие человека (страница, API),
scheduler — поток расписания. Запись под замком; сбой записи не роняет того,
кто пишет: аналитика не должна останавливать выпуск.
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
import time
import uuid
from pathlib import Path

FILE = "events.jsonl"
SYSTEM, HUMAN, SCHEDULER = "system", "human", "scheduler"
SCHEDULER_THREAD = "studio-schedule"  # имя потока schedule.Scheduler
FIELDS = (
    "project_id",
    "scenario",
    "step",
    "status_from",
    "status_to",
    "duration_s",
    "attempt",
    "error_class",
    "channel_id",
    "publication_id",
    "metric",
    "value",
    "age_h",
    "source",
)
_LOCK = threading.Lock()  # один на процесс: журнал пишут студия, отклик и бэкфилл
LOCK = _LOCK  # и журнал комплаенса (core/audit.py): один механизм записи


def append_jsonl(path: Path, record: dict, mode: int | None = None) -> bool:
    """Дописать строку JSON в конец файла под общим замком; mode — права нового файла (0600).
    Ошибка записи — False, без исключения: журнал не роняет того, кто пишет."""
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644 if mode is None else mode)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
        return True
    except Exception:  # noqa: BLE001
        return False


def actor(default: str = HUMAN) -> str:
    """Кто действует: в потоке расписания — scheduler, иначе default (запрос со страницы — human)."""
    return SCHEDULER if threading.current_thread().name == SCHEDULER_THREAD else default


class EventLog:
    """Журнал событий; path=None — ничего не пишет (студия без журнала, тесты)."""

    def __init__(self, path: Path | None) -> None:
        self.path = path

    def emit(self, event: str, *, actor: str = SYSTEM, ts: float | None = None, event_id: str | None = None, **fields):
        """Дописать событие; вернуть его. Ошибка записи — None, без исключения."""
        try:
            props = dict(fields.pop("props", None) or {})
            record = {
                "event_id": event_id or uuid.uuid4().hex,
                "ts": round(time.time() if ts is None else ts, 3),
                "event": event,
                "actor": actor,
            }
            for key, value in fields.items():
                if value is None:
                    continue
                if key in FIELDS:
                    record[key] = round(value, 3) if isinstance(value, float) else value
                else:
                    props[key] = value
            if props:
                record["props"] = props
            if self.path is None:
                return record
            return record if append_jsonl(self.path, record) else None
        except Exception:  # noqa: BLE001 — журнал не должен ронять выпуск
            return None

    def read(self) -> list[dict]:
        """Все события; битые строки (оборванная запись) пропускаются."""
        if self.path is None:
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(json.JSONDecodeError):
                item = json.loads(line)
                if isinstance(item, dict):
                    out.append(item)
        return out
