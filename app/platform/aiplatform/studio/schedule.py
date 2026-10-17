"""Расписание сценариев: дайджест сам в заданное время и ежедневные работы (отклик, уборка).

Поток раз в AIP_SCHEDULE_TICK_S секунд (по умолчанию 30) зовёт tick(сейчас).
Наступило время из настроек, а сегодня дайджест ещё не запускали — создать
выпуск за вчера (или за сегодня) и запустить. Если платформа была выключена
во время запуска, он догоняется в тот же день, но не на следующий. Дата
последнего запуска каждой работы — в `<data>/studio/schedule.json`: упавшая
работа не повторяется каждые полминуты.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from aiplatform.core.http import HttpError

STATE = "schedule.json"
DEFAULT_TICK_S = 30.0


class Scheduler:
    def __init__(self, module, state_dir: Path, tick_s: float = DEFAULT_TICK_S) -> None:
        self.module = module  # StudioModule: create, notify, настройки
        self.path = state_dir / STATE
        self.tick_s = tick_s
        # Ежедневные работы: имя → функция; идут один раз в день, после времени дайджеста не ждут.
        self.daily: dict[str, Callable[[], str | None]] = {}
        self._lock = threading.Lock()
        self._lock_state = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.tick_s > 0 and self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="studio-schedule", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            with contextlib.suppress(Exception):  # сбой расписания не роняет платформу
                self.tick(datetime.now())

    # --- состояние ---

    def state(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def note(self, name: str, value: str) -> None:
        """Записать отметку (например, когда последний раз обновлялся отклик)."""
        self._mark(name, value)

    def _mark(self, name: str, value: str) -> None:
        with self._lock_state:
            data = self.state() | {name: value}
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp.replace(self.path)

    # --- работа ---

    def tick(self, now: datetime) -> list[str]:
        """Сделать всё, чему пришло время; вернуть, что сделано (для лога и тестов)."""
        done: list[str] = []
        with self._lock:
            today = now.date().isoformat()
            plan = self.module.ctx.config.settings.load()["schedule"]["digest"]
            if plan["enabled"] and now.strftime("%H:%M") >= plan["time"] and self.state().get("digest") != today:
                self._mark("digest", today)
                done.append(self._digest(now, plan["day"]))
            for name, job in self.daily.items():
                if self.state().get(name) == today:
                    continue
                self._mark(name, today)
                with contextlib.suppress(Exception):
                    result = job()
                    if result:
                        done.append(result)
        return done

    def _digest(self, now: datetime, which: str) -> str:
        day = (now.date() - timedelta(days=1) if which == "yesterday" else now.date()).isoformat()
        archived = [p for p in self.module.studio.list(archived=True) if self.module.source_of(p) == f"digest:{day}"]
        if archived:  # убранный в архив выпуск за этот день — решение человека, заново не собираем
            return f"дайджест за {day}: есть в архиве"
        try:
            project = self.module.create({"scenario": "digest", "day": day, "run": True})
        except HttpError as exc:
            existing = exc.extra.get("existing")
            if existing:
                return f"дайджест за {day}: уже есть «{existing['title']}»"
            self.module.notify("Дайджест по расписанию не запустился", exc.message, important=True)
            return f"дайджест за {day}: {exc.message}"
        self.module.notify("Дайджест запущен по расписанию", project["title"], important=False)
        return f"дайджест за {day}: запущен {project['id']}"
