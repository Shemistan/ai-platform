"""Проекты сценариев: шаги, их состояние на диске, зависимости и запуск в фоне.

Проект — каталог `data/studio/projects/<id>/` с `project.json` и файлами
шагов. Шаг — функция из steps.py: пишет свои файлы и либо завершается
(done), либо просит человека (Waiting), либо пропускается (Skip), либо
падает (StudioError). «Запустить всё» идёт по шагам, пропуская готовые,
до первого ожидания или ошибки. Правка результата шага сбрасывает
зависящие от него шаги в «не начат».
"""

from __future__ import annotations

import copy
import json
import re
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aiplatform.core import runner
from aiplatform.studio.llm import StudioError

PENDING, RUNNING, DONE, FAILED, WAITING, SKIPPED = "pending", "running", "done", "failed", "waiting", "skipped"
FINISHED = (DONE, SKIPPED)
LOG_TAIL = 300
_ID = re.compile(r"^[0-9a-f]{12}$")


@dataclass(frozen=True)
class Step:
    id: str
    label: str
    deps: tuple[str, ...] = ()


STEPS = (
    Step("text", "Текст"),
    Step("poster_prompt", "Промпт постера", ("text",)),
    Step("poster", "Постер — первый кадр", ("poster_prompt",)),
    Step("plan", "Слайды и промпты картинок", ("text",)),
    Step("images", "Картинки слайдов", ("plan",)),
    Step("clips", "Видео для слайдов", ("plan",)),
    Step("voice", "Озвучка", ("text",)),
    Step("render", "Сборка видео 16:9", ("poster", "plan", "images", "clips", "voice")),
    Step("short", "Шортс 9:16", ("text", "poster", "images", "clips")),
    Step("publish", "Публикация", ("render", "short")),
)
STEP_IDS = tuple(s.id for s in STEPS)
LABELS = {s.id: s.label for s in STEPS}


class Waiting(Exception):  # noqa: N818 — это не ошибка, а «нужен человек»
    """Шаг ждёт человека: загрузить файл, проверить текст, нажать «Опубликовать»."""


class Skip(Exception):  # noqa: N818
    """Шаг не нужен при этих настройках (например, шортс выключен)."""


class Conflict(Exception):
    """Действие сейчас невозможно: проект уже запущен."""


def dependents(step_id: str) -> list[str]:
    """Все шаги, которые прямо или через другие зависят от step_id."""
    out: list[str] = []
    frontier = [step_id]
    while frontier:
        current = frontier.pop()
        for step in STEPS:
            if current in step.deps and step.id not in out:
                out.append(step.id)
                frontier.append(step.id)
    return [s for s in STEP_IDS if s in out]


class StepContext:
    """Что видит шаг: проект (копия), его каталог, отмена, лог и прогресс."""

    def __init__(self, studio: Studio, project: dict, step_id: str, cancel: threading.Event) -> None:
        self.studio = studio
        self.project = project
        self.step = step_id
        self.cancel = cancel
        self.dir = studio.project_dir(project["id"])

    @property
    def settings(self) -> dict:
        return self.project["settings"]

    @property
    def state(self) -> dict:
        return self.project["steps"][self.step]

    def log(self, message: str) -> None:
        self.studio._log(self.project["id"], f"[{LABELS[self.step]}] {message}")

    def progress(self, stage: str, percent: float | None = None) -> None:
        self.studio._progress(self.project["id"], self.step, stage, percent)

    def remember(self, **values) -> None:
        """Сохранить в состоянии шага (id задачи вкладки и т.п.)."""
        self.studio._remember(self.project["id"], self.step, values)

    def set_title(self, title: str) -> None:
        self.studio._set_title(self.project["id"], title)

    def path(self, name: str) -> Path:
        return self.dir / name

    def read(self, name: str) -> str:
        try:
            return (self.dir / name).read_text(encoding="utf-8")
        except OSError:
            return ""

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
        return path


StepFn = Callable[[StepContext], None]


class Studio:
    def __init__(self, root: Path, steps: dict[str, StepFn], services: object = None) -> None:
        self.root = root
        self.steps = steps
        self.services = services  # то, чем шаги пользуются: вкладки, LLM, рендер — см. steps.Services
        self._lock = threading.Lock()
        self._projects: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        root.mkdir(parents=True, exist_ok=True)
        self._load()

    # --- хранение ---

    def project_dir(self, project_id: str) -> Path:
        return self.root / project_id

    def _save(self, project: dict) -> None:
        folder = self.project_dir(project["id"])
        folder.mkdir(parents=True, exist_ok=True)
        project["updated_at"] = time.time()
        tmp = folder / "project.json.tmp"
        tmp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / "project.json")

    def _load(self) -> None:
        for path in self.root.glob("*/project.json"):
            if not _ID.match(path.parent.name):
                continue
            try:
                project = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            changed = False
            for step in STEPS:  # выпуск из версии, где этого шага ещё не было
                if step.id not in project["steps"]:
                    # В старом выпуске настройка нового шага выключена: шаг сразу пропущен.
                    project["steps"][step.id] = _fresh_state() | {"status": SKIPPED, "note": "Не было в этой версии."}
                    changed = True
            for state in project["steps"].values():
                if state["status"] == RUNNING:
                    state.update(status=FAILED, error="Сервис был перезапущен во время шага.", progress=None)
                    changed = True
            if project.get("running"):
                project["running"] = False
                changed = True
            if changed:
                self._save(project)
            self._projects[project["id"]] = project

    # --- чтение ---

    def get(self, project_id: str) -> dict | None:
        with self._lock:
            project = self._projects.get(project_id)
            return copy.deepcopy(project) if project else None

    def list(self, scenario: str | None = None) -> list[dict]:
        with self._lock:
            items = [p for p in self._projects.values() if scenario in (None, p["scenario"])]
            items.sort(key=lambda p: p["created_at"], reverse=True)
            return [
                {
                    "id": p["id"],
                    "scenario": p["scenario"],
                    "title": p["title"],
                    "created_at": p["created_at"],
                    "running": p["running"],
                    "status": summary(p),
                }
                for p in items
            ]

    # --- запись ---

    def create(self, scenario: str, title: str, settings: dict, extra: dict | None = None) -> dict:
        project_id = uuid.uuid4().hex[:12]
        project = {
            "id": project_id,
            "scenario": scenario,
            "title": title,
            "created_at": time.time(),
            "updated_at": time.time(),
            "settings": settings,
            "running": False,
            "current": None,
            "steps": {s.id: _fresh_state() for s in STEPS},
            "log": [],
            **(extra or {}),
        }
        with self._lock:
            self._projects[project_id] = project
            self._save(project)
            return copy.deepcopy(project)

    def update(self, project_id: str, mutate: Callable[[dict], set[str]]) -> dict:
        """Изменить проект не во время запуска. mutate возвращает шаги, которые надо сбросить."""
        with self._lock:
            project = self._require(project_id)
            if project["running"]:
                raise Conflict("Проект сейчас выполняется — дождись или останови его.")
            for step_id in mutate(project):
                self._reset(project, step_id, include_self=True)
            self._save(project)
            return copy.deepcopy(project)

    def mark_done(self, project_id: str, step_id: str, note: str | None = None) -> dict:
        """Результат шага дал человек (правка, загрузка): шаг готов, зависящие — заново."""
        with self._lock:
            project = self._require(project_id)
            if project["running"]:
                raise Conflict("Проект сейчас выполняется — дождись или останови его.")
            self._reset(project, step_id, include_self=False)
            state = project["steps"][step_id]
            state.update(status=DONE, error=None, note=note, progress=None, updated_at=time.time())
            self._save(project)
            return copy.deepcopy(project)

    def approve(self, project_id: str, step_id: str) -> dict:
        with self._lock:
            project = self._require(project_id)
            state = project["steps"][step_id]
            if state["status"] != WAITING:
                raise Conflict("Этот шаг ничего не ждёт.")
            state["approved"] = True
            self._save(project)
        return self.run(project_id)

    def run(self, project_id: str, only: str | None = None) -> dict:
        """Запустить в фоне: все незавершённые шаги по порядку или один шаг заново (only)."""
        with self._lock:
            project = self._require(project_id)
            if project["running"]:
                raise Conflict("Проект уже выполняется.")
            if only is not None:
                self._reset(project, only, include_self=True)
            project["running"] = True
            self._save(project)
            cancel = threading.Event()
            self._cancels[project_id] = cancel
            thread = threading.Thread(
                target=self._run, args=(project_id, only, cancel), name=f"studio-{project_id}", daemon=True
            )
            self._threads[project_id] = thread
            thread.start()
            return copy.deepcopy(project)

    def cancel(self, project_id: str) -> dict:
        with self._lock:
            project = self._require(project_id)
            event = self._cancels.get(project_id)
            if event:
                event.set()
            return copy.deepcopy(project)

    def join(self, project_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(project_id)
        if thread:
            thread.join(timeout)

    def close(self) -> None:
        with self._lock:
            events = list(self._cancels.values())
            threads = list(self._threads.values())
        for event in events:
            event.set()
        for thread in threads:
            thread.join(timeout=15)

    # --- внутреннее ---

    def _require(self, project_id: str) -> dict:
        project = self._projects.get(project_id)
        if project is None:
            raise KeyError(project_id)
        return project

    def _reset(self, project: dict, step_id: str, *, include_self: bool) -> None:
        targets = ([step_id] if include_self else []) + dependents(step_id)
        for target in targets:
            state = project["steps"][target]
            keep = {k: state[k] for k in ("job", "keep") if k in state}
            project["steps"][target] = _fresh_state() | keep

    def _log(self, project_id: str, message: str) -> None:
        with self._lock:
            project = self._projects[project_id]
            stamp = time.strftime("%H:%M:%S")
            project["log"].append(f"{stamp} {message}")
            del project["log"][:-LOG_TAIL]

    def _progress(self, project_id: str, step_id: str, stage: str, percent: float | None) -> None:
        with self._lock:
            state = self._projects[project_id]["steps"][step_id]
            state["progress"] = {"stage": stage, "percent": percent}

    def _remember(self, project_id: str, step_id: str, values: dict) -> None:
        with self._lock:
            project = self._projects[project_id]
            project["steps"][step_id].update(values)
            self._save(project)

    def _set_title(self, project_id: str, title: str) -> None:
        with self._lock:
            project = self._projects[project_id]
            project["title"] = title
            self._save(project)

    def _set(self, project_id: str, step_id: str, **values) -> dict:
        with self._lock:
            project = self._projects[project_id]
            project["steps"][step_id].update(values, updated_at=time.time())
            if values.get("status") == RUNNING:
                project["current"] = step_id
            self._save(project)
            return copy.deepcopy(project)

    def _run(self, project_id: str, only: str | None, cancel: threading.Event) -> None:
        try:
            for step in STEPS:
                if only is not None and step.id != only:
                    continue
                if cancel.is_set():
                    break
                with self._lock:
                    status = self._projects[project_id]["steps"][step.id]["status"]
                if only is None and status in FINISHED:
                    continue
                if not self._run_step(project_id, step, cancel):
                    break
        finally:
            with self._lock:
                project = self._projects[project_id]
                project["running"] = False
                project["current"] = None
                self._cancels.pop(project_id, None)
                self._save(project)

    def _run_step(self, project_id: str, step: Step, cancel: threading.Event) -> bool:
        """True — можно идти дальше."""
        project = self._set(project_id, step.id, status=RUNNING, error=None, note=None, progress=None)
        ctx = StepContext(self, project, step.id, cancel)
        self._log(project_id, f"▶ {step.label}")
        try:
            self.steps[step.id](ctx)
        except Waiting as exc:
            self._set(project_id, step.id, status=WAITING, note=str(exc), progress=None)
            self._log(project_id, f"⏸ {step.label}: {exc}")
            return False
        except Skip as exc:
            self._set(project_id, step.id, status=SKIPPED, note=str(exc), progress=None)
            self._log(project_id, f"↷ {step.label}: {exc}")
            return True
        except runner.Canceled:
            self._set(project_id, step.id, status=PENDING, note="Остановлено.", progress=None)
            self._log(project_id, f"■ {step.label}: остановлено")
            return False
        except StudioError as exc:
            self._set(project_id, step.id, status=FAILED, error=str(exc), progress=None)
            self._log(project_id, f"✗ {step.label}: {exc}")
            return False
        except Exception as exc:  # noqa: BLE001 — сбой шага не должен уронить поток проекта
            self._set(project_id, step.id, status=FAILED, error=f"Внутренняя ошибка: {exc!r}", progress=None)
            self._log(project_id, f"✗ {step.label}: {exc!r}")
            return False
        if cancel.is_set():
            self._set(project_id, step.id, status=PENDING, note="Остановлено.", progress=None)
            return False
        self._set(project_id, step.id, status=DONE, progress=None)
        self._log(project_id, f"✓ {step.label}")
        return True


def _fresh_state() -> dict:
    return {"status": PENDING, "error": None, "note": None, "progress": None, "updated_at": None}


def summary(project: dict) -> str:
    """Одно слово о проекте для списка: идёт, ждёт, ошибка, готово, черновик."""
    statuses = [s["status"] for s in project["steps"].values()]
    if project.get("running"):
        return RUNNING
    if FAILED in statuses:
        return FAILED
    if WAITING in statuses:
        return WAITING
    if all(s in FINISHED for s in statuses):
        return DONE
    return PENDING
