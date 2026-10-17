"""Расписание сценариев: дайджест сам в заданное время и ежедневные работы (отклик, уборка).

Поток раз в AIP_SCHEDULE_TICK_S секунд (по умолчанию 30) зовёт tick(сейчас).
Наступило время из настроек, а сегодня дайджест ещё не удался — создать
выпуск за вчера (или за сегодня) и запустить. Если платформа была выключена
во время запуска, он догоняется в тот же день, но не на следующий.

Итог дайджеста — в `<data>/studio/schedule.json`:
`{"digest": {"day", "result": "ok|failed", "error", "at", "attempts"}}`. День
помечается удачным только после `create`; сбой — уведомление, трассировка в
лог и повтор через RETRY_PAUSE, не больше MAX_ATTEMPTS раз за день. От
двойного выпуска защищает дедупликация по `source` в `create`.

Ежедневные работы помечаются датой до запуска (упавшая не повторяется каждые
полминуты), но сбой виден: трассировка в лог и важное уведомление.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from aiplatform.config import pick
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.http import HttpError
from aiplatform.core.log import event as log_event

STATE = "schedule.json"
DEFAULT_TICK_S = 30.0
MAX_ATTEMPTS = 3  # попыток дайджеста за день
RETRY_PAUSE = timedelta(minutes=15)

log = logging.getLogger("aiplatform.schedule")


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
        self.started_at: float | None = None
        self.last_tick_at: float | None = None  # time.time() последнего тика потока

    def start(self) -> None:
        if self.tick_s > 0 and self._thread is None:
            self.started_at = time.time()
            self._thread = threading.Thread(target=self._loop, name="studio-schedule", daemon=True)
            self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_s):
            self.last_tick_at = time.time()  # поток жив — /api/ready смотрит на эту отметку
            try:
                done = self.tick(datetime.now())
            except Exception:  # сбой расписания не роняет платформу, но виден в логе
                log.exception("schedule.tick_failed")
                continue
            log_event("schedule.tick", result=done or "nothing")
            self._heartbeat()

    def alive(self, now: float | None = None) -> bool:
        """Поток расписания тикал не позже двух интервалов назад (до первого тика — по времени старта)."""
        if self._thread is None or not self._thread.is_alive():
            return False
        last = self.last_tick_at or self.started_at or 0.0
        return (now or time.time()) - last <= 2 * self.tick_s

    def _heartbeat(self) -> None:
        """Пинг внешнего heartbeat-сервиса (healthchecks.io) после удачного тика; URL — AIP_HEARTBEAT_URL."""
        url = pick(self.module.ctx.config.env, "AIP_HEARTBEAT_URL")
        if not url:
            return
        try:
            with urllib.request.urlopen(url, timeout=10):  # noqa: S310 — адрес задаёт владелец
                pass
        except OSError as exc:  # сеть пропала — не повод ронять расписание; молчание заметит сам сервис
            log.warning("heartbeat не ушёл: %s", type(exc).__name__)

    # --- состояние ---

    def state(self) -> dict:
        """Отметки из файла и, если поток уже тикал, last_tick_at (только в памяти)."""
        data = self._read()
        if self.last_tick_at:
            data["last_tick_at"] = datetime.fromtimestamp(self.last_tick_at).isoformat(timespec="seconds")
        return data

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def note(self, name: str, value: str) -> None:
        """Записать отметку (например, когда последний раз обновлялся отклик)."""
        self._mark(name, value)

    def _mark(self, name: str, value: str | dict) -> None:
        with self._lock_state:
            data = self._read() | {name: value}
            atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def digest_run(self) -> dict:
        """Последний запуск дайджеста; старая запись строкой-датой считается удачной."""
        run = self.state().get("digest")
        if isinstance(run, str):
            return {"day": run, "result": "ok", "error": None, "at": None, "attempts": 1} if run else {}
        return run if isinstance(run, dict) else {}

    # --- работа ---

    def tick(self, now: datetime) -> list[str]:
        """Сделать всё, чему пришло время; вернуть, что сделано (для лога и тестов)."""
        done: list[str] = []
        with self._lock:
            today = now.date().isoformat()
            plan = self.module.ctx.config.settings.load()["schedule"]["digest"]
            if plan["enabled"] and now.strftime("%H:%M") >= plan["time"] and self._digest_due(now):
                done.append(self._digest(now, plan["day"]))
            for name, job in self.daily.items():
                if self.state().get(name) == today:
                    continue
                self._mark(name, today)
                result = self._daily(name, job)
                if result:
                    done.append(result)
        return done

    def _digest_due(self, now: datetime) -> bool:
        run = self.digest_run()
        if run.get("day") != now.date().isoformat():
            return True
        if run.get("result") != "failed" or run.get("attempts", 0) >= MAX_ATTEMPTS:
            return False
        try:
            last = datetime.fromisoformat(run["at"])
        except (KeyError, TypeError, ValueError):
            return True
        return now - last >= RETRY_PAUSE

    def _daily(self, name: str, job: Callable[[], str | None]) -> str | None:
        try:
            return job()
        except Exception as exc:
            log.exception("ежедневная работа %s упала", name)
            self.module.notify(f"Ежедневная работа «{name}» упала", _brief(exc), important=True)
            return f"{name}: сбой — {_brief(exc)}"

    def _digest(self, now: datetime, which: str) -> str:
        today = now.date().isoformat()
        run = self.digest_run()
        attempts = (run.get("attempts", 0) if run.get("day") == today else 0) + 1
        day = (now.date() - timedelta(days=1) if which == "yesterday" else now.date()).isoformat()

        def finish(error: str | None) -> None:
            self._mark(
                "digest",
                {
                    "day": today,
                    "result": "failed" if error else "ok",
                    "error": error,
                    "at": now.isoformat(timespec="seconds"),
                    "attempts": attempts,
                },
            )

        try:
            source = f"digest:{day}"
            archived = [p for p in self.module.studio.list(archived=True) if self.module.source_of(p) == source]
            if archived:  # убранный в архив выпуск за этот день — решение человека, заново не собираем
                finish(None)
                return f"дайджест за {day}: есть в архиве"
            project = self.module.create({"scenario": "digest", "day": day, "run": True})
        except HttpError as exc:
            existing = exc.extra.get("existing")
            if existing:
                finish(None)
                return f"дайджест за {day}: уже есть «{existing['title']}»"
            return self._digest_failed(day, attempts, exc.message, finish)
        except Exception as exc:  # диск, настройки, мост narrator — что угодно, но не молча
            log.exception("дайджест за %s по расписанию упал", day)
            return self._digest_failed(day, attempts, _brief(exc), finish)
        finish(None)
        self.module.notify("Дайджест запущен по расписанию", project["title"], important=False)
        return f"дайджест за {day}: запущен {project['id']}"

    def _digest_failed(self, day: str, attempts: int, error: str, finish: Callable[[str], None]) -> str:
        finish(error)
        if attempts < MAX_ATTEMPTS:
            minutes = int(RETRY_PAUSE.total_seconds() // 60)
            more = f"Попытка {attempts} из {MAX_ATTEMPTS}, следующая через {minutes} мин."
        else:
            more = f"Попытка {attempts} из {MAX_ATTEMPTS}, больше сегодня не пробую — запусти вручную."
        self.module.notify("Дайджест по расписанию не запустился", f"За {day}: {error}. {more}", important=True)
        return f"дайджест за {day}: {error}"


def _brief(exc: BaseException) -> str:
    """Ошибка одной строкой для уведомления: тип и текст."""
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
