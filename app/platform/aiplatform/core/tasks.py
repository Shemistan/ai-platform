"""Фоновые задачи вкладок: команда движка с логом, очередь по одной, история на диске.

Задача — одна команда (озвучить статью, собрать источник, скачать модели,
позвать агента). Вкладка ставит её в очередь и показывает ход по логу.
Задачи одной очереди идут по одной: они пишут в одну библиотеку. Тяжёлые
(gpu=…) ещё и ждут общий GPU-замок, как задачи «Фото» и «Видео».
"""

from __future__ import annotations

import copy
import json
import queue
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from aiplatform.core import runner
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.gate import Gate

ACTIVE = ("queued", "running")
LOG_TAIL = 400
KEEP = 60  # столько завершённых задач помнит очередь
_ID = re.compile(r"^[0-9a-f]{12}$")


@dataclass
class Spec:
    kind: str  # что за задача: synth, collect, models… — вкладка фильтрует по нему
    title: str
    argv: list[str]
    cwd: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    gpu: str | None = None  # имя в GPU-замке; None — замок не нужен
    target: str = ""  # к чему относится: id статьи, источника…
    hint: str = runner.DEFAULT_MISSING_HINT
    progress: Callable[[str], str | None] | None = None  # строка лога → этап; None — без смены
    on_done: Callable[[dict], None] | None = None  # после успешного конца, в потоке очереди


class Tasks:
    def __init__(self, folder: Path, gate: Gate | None = None) -> None:
        self._folder = folder
        self._gate = gate
        self._lock = threading.Lock()
        self._tasks: dict[str, dict] = {}
        self._specs: dict[str, Spec] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        folder.mkdir(parents=True, exist_ok=True)
        self._load()
        self._worker = threading.Thread(target=self._work, name=f"tasks-{folder.name}", daemon=True)
        self._worker.start()

    # --- чтение ---

    def get(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            return self._view(task) if task else None

    def list(self, kind: str | None = None, target: str | None = None) -> list[dict]:
        """Задачи, новые сверху; лог отдаётся только в get()."""
        with self._lock:
            items = [t for t in self._tasks.values() if kind in (None, t["kind"]) and target in (None, t["target"])]
            items.sort(key=lambda t: t["created_at"], reverse=True)
            return [{k: v for k, v in self._view(t).items() if k != "log"} | {"last": _last(t)} for t in items]

    def busy(self, kind: str | None = None, target: str | None = None) -> dict | None:
        """Идущая или ждущая задача этого вида (и цели); None — свободно."""
        with self._lock:
            for t in self._tasks.values():
                if t["status"] in ACTIVE and kind in (None, t["kind"]) and target in (None, t["target"]):
                    return self._view(t)
        return None

    def _view(self, task: dict) -> dict:
        view = copy.deepcopy(task)
        end = task["finished_at"] or time.time()
        view["elapsed_s"] = round(end - task["started_at"], 1) if task["started_at"] else 0.0
        if task["status"] == "queued":
            view["queue_position"] = sum(
                t["status"] in ACTIVE and t["created_at"] < task["created_at"] for t in self._tasks.values()
            )
        return view

    # --- запись ---

    def start(self, spec: Spec) -> dict:
        task_id = uuid.uuid4().hex[:12]
        task = {
            "id": task_id,
            "kind": spec.kind,
            "title": spec.title,
            "target": spec.target,
            "status": "queued",
            "stage": "В очереди",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "log": [],
        }
        with self._lock:
            self._tasks[task_id] = task
            self._specs[task_id] = spec
            self._cancels[task_id] = threading.Event()
            self._save(task)
            self._prune()
            view = self._view(task)
        self._queue.put(task_id)
        return view

    def cancel(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            if task["status"] == "queued":
                self._finish(task, "canceled")
            elif task["status"] == "running":
                task["stage"] = "Отмена…"
            event = self._cancels.get(task_id)
            if event:
                event.set()
            return self._view(task)

    def wait(self, task_id: str, timeout: float = 60) -> dict | None:
        """Для тестов: дождаться конца задачи."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            task = self.get(task_id)
            if task is None or task["status"] not in ACTIVE:
                return task
            time.sleep(0.05)
        return self.get(task_id)

    def close(self) -> None:
        with self._lock:
            for event in self._cancels.values():
                event.set()
        self._queue.put(None)
        self._worker.join(timeout=15)

    # --- внутреннее ---

    def _save(self, task: dict) -> None:
        atomic_write(self._folder / f"{task['id']}.json", json.dumps(task, ensure_ascii=False))

    def _load(self) -> None:
        for path in self._folder.glob("*.json"):
            if not _ID.match(path.stem):
                continue
            try:
                task = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if task.get("status") in ACTIVE:
                task.update(status="failed", error="Сервис был перезапущен во время задачи.", stage="Ошибка")
                task["finished_at"] = task["finished_at"] or time.time()
                self._save(task)
            self._tasks[task["id"]] = task

    def _prune(self) -> None:
        done = sorted((t for t in self._tasks.values() if t["status"] not in ACTIVE), key=lambda t: t["created_at"])
        for task in done[:-KEEP]:
            self._tasks.pop(task["id"], None)
            (self._folder / f"{task['id']}.json").unlink(missing_ok=True)

    def _finish(self, task: dict, status: str, error: str | None = None) -> None:
        task["status"] = status
        task["finished_at"] = time.time()
        task["error"] = error
        task["stage"] = {"done": "Готово", "failed": "Ошибка", "canceled": "Отменено"}[status]
        self._save(task)

    def _work(self) -> None:
        while (task_id := self._queue.get()) is not None:
            with self._lock:
                task = self._tasks.get(task_id)
                spec = self._specs.pop(task_id, None)
                cancel = self._cancels.get(task_id)
                if task is None or spec is None or task["status"] != "queued":
                    self._cancels.pop(task_id, None)
                    continue
                task.update(status="running", started_at=time.time(), stage="Запуск")
                self._save(task)
            self._run(task, spec, cancel)
            with self._lock:
                self._cancels.pop(task_id, None)

    def _run(self, task: dict, spec: Spec, cancel: threading.Event) -> None:
        def on_line(line: str) -> None:
            text = line.rstrip()
            if not text.strip():
                return
            with self._lock:
                task["log"].append(text)
                del task["log"][:-LOG_TAIL]
                stage = spec.progress(text) if spec.progress else None
                if stage and not cancel.is_set():
                    task["stage"] = stage

        def on_wait(holder: str) -> None:
            with self._lock:
                task["stage"] = f"Ждёт GPU (занят: {holder})"
                self._save(task)

        try:
            if spec.gpu and self._gate is not None:
                with self._gate.hold(spec.gpu, on_wait, cancel):
                    with self._lock:
                        task["stage"] = "Идёт"
                    runner.run(spec.argv, on_line, cancel, cwd=spec.cwd, env=spec.env, missing_hint=spec.hint)
            else:
                with self._lock:
                    task["stage"] = "Идёт"
                runner.run(spec.argv, on_line, cancel, cwd=spec.cwd, env=spec.env, missing_hint=spec.hint)
            if spec.on_done:
                spec.on_done(self._view(task))
        except runner.Canceled:
            with self._lock:
                self._finish(task, "canceled")
        except runner.RunFailed as exc:
            with self._lock:
                self._finish(task, "failed", _error_text(str(exc), task["log"]))
        except Exception as exc:  # noqa: BLE001 — очередь не должна умереть из-за одной задачи
            with self._lock:
                self._finish(task, "failed", f"Внутренняя ошибка: {exc!r}")
        else:
            with self._lock:
                self._finish(task, "done")


def _last(task: dict) -> str:
    """Последняя строка лога — в списке задач вместо всего лога."""
    return task["log"][-1] if task["log"] else ""


def _error_text(reason: str, log: list[str]) -> str:
    for line in reversed(log):
        text = line.strip()
        if text and not text.startswith(("[", "=")):
            return f"{reason} Последнее сообщение: {text}"
    return reason
