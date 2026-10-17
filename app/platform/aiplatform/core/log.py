"""Лог процесса: JSON-строки в `<data>/logs/platform.log` с ротацией по размеру.

Чтобы ответить на «почему вчера не вышел дайджест»: старт и стоп процесса,
тики расписания, задачи и шаги выпусков с длительностью, публикации, уборка,
необработанные ошибки HTTP и запросы, кроме опросов. Строка — объект
`{ts, level, event, project, step, task, duration_s, error, …}`.

Модули пишут через `event("step.done", project=…, duration_s=…)` в логгер
`aiplatform` (и его дочерние, как `aiplatform.schedule`). Файл подключает
`setup()` — из `server.main`; без него события идут обычным путём logging
(в тестах не шумят). Под launchd (AIP_LOG_STDERR=1 в plist) строки
дублируются в stderr.
"""

from __future__ import annotations

import contextlib
import json
import logging
import logging.handlers
import sys
import time
from pathlib import Path

NAME = "aiplatform"
FILE = "platform.log"
MAX_BYTES = 10 * 2**20
BACKUPS = 5
# Поля строки в этом порядке; остальное, что передали в event(), — следом.
FIELDS = ("project", "step", "task", "duration_s", "error")
_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}

logger = logging.getLogger(NAME)
logger.addHandler(logging.NullHandler())  # без setup() (тесты, импорт) события не печатаются в stderr
_handlers: list[logging.Handler] = []


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "event": record.getMessage(),
        }
        if record.name != NAME:
            out["logger"] = record.name
        extra = {k: v for k, v in vars(record).items() if k not in _RESERVED and not k.startswith("_")}
        for key in FIELDS:
            if extra.get(key) is not None:
                out[key] = extra.pop(key)
        out |= {k: v for k, v in extra.items() if v is not None}
        if record.exc_info:
            out["error"] = (
                (out.get("error") or "") + ("\n" if out.get("error") else "") + self.formatException(record.exc_info)
            )
        return json.dumps(out, ensure_ascii=False, default=str)


def setup(data_dir: Path, *, stderr: bool = False, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> Path:
    """Подключить файл (повторный вызов заменяет прежние обработчики). Вернуть путь лога."""
    close()
    path = data_dir / "logs" / FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(path, maxBytes=max_bytes, backupCount=backups, encoding="utf-8")
    ]
    if stderr:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        _handlers.append(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # строки уже в файле — корневой логгер их не дублирует
    return path


def close() -> None:
    """Отключить обработчики setup(): тестам и при остановке процесса."""
    while _handlers:
        handler = _handlers.pop()
        logger.removeHandler(handler)
        handler.close()
    logger.propagate = True


def event(name: str, *, level: int = logging.INFO, exc: bool = False, **fields) -> None:
    """Событие одной строкой; сбой записи лога не мешает работе."""
    clash = {k: fields.pop(k) for k in list(fields) if k in _RESERVED}
    fields |= {f"{k}_": v for k, v in clash.items()}
    with contextlib.suppress(Exception):
        logger.log(level, name, exc_info=exc, extra=fields)


def rotate(path: Path, max_bytes: int = MAX_BYTES, backups: int = BACKUPS) -> None:
    """Ротация файла, который пишут не через logging (notifications.log): file → file.1 → … → file.N."""
    try:
        if path.stat().st_size < max_bytes:
            return
    except OSError:
        return
    with contextlib.suppress(OSError):
        for n in range(backups - 1, 0, -1):
            older = path.with_name(f"{path.name}.{n}")
            if older.exists():
                older.replace(path.with_name(f"{path.name}.{n + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
