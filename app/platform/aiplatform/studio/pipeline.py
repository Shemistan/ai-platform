"""Проекты сценариев: шаги, их состояние на диске, зависимости и запуск в фоне.

Проект — каталог `data/studio/projects/<id>/` с `project.json` и файлами
шагов. Шаг — функция из steps.py: пишет свои файлы и либо завершается
(done), либо просит человека (Waiting), либо пропускается (Skip), либо
падает (StudioError). «Запустить всё» выполняет каждый шаг, у которого
готово всё, от чего он зависит: ожидание или ошибка одного шага держат
только зависящие от него, остальные идут дальше. Пока выпуск запущен,
человек может загружать файлы и подтверждать шаги, которые сейчас не
выполняются; как только шаг готов, запуск подхватывает его зависящие сам.
Правка результата шага сбрасывает зависящие от него шаги в «не начат».
Каждая смена состояния шага и действие человека пишутся в журнал событий
(core/events.py): состояние в project.json перезаписывается, журнал — нет.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import re
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from aiplatform.core import cleanup, events, runner
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.log import event as log_event
from aiplatform.studio.llm import StudioError

PENDING, RUNNING, DONE, FAILED, WAITING, SKIPPED = "pending", "running", "done", "failed", "waiting", "skipped"
FINISHED = (DONE, SKIPPED)
LOG_TAIL = 300
INTERRUPTED = "Сервис был перезапущен во время шага."
NO_AUTO_RESUME = ("publish",)  # повтор публикации — только руками (см. BE-001)
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
    Step("clips", "Ролики", ("plan",)),
    Step("voice", "Озвучка", ("text",)),
    Step("render", "Сборка видео", ("poster", "plan", "images", "clips", "voice")),
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


class ProjectNotFound(LookupError):  # noqa: N818
    """Нет выпуска с таким id. Свой тип: KeyError из бага в коде не должен становиться 404."""


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


STEP_BY_ID = {s.id: s for s in STEPS}


def blockers(project: dict, step_id: str) -> list[str]:
    """Шаги, от которых зависит step_id и которые ещё не готовы."""
    return [d for d in STEP_BY_ID[step_id].deps if project["steps"][d]["status"] not in FINISHED]


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

    def emit(self, event: str, **fields) -> None:
        """Событие в журнал от имени шага (публикация и т.п.)."""
        self.studio._emit(self.project, event, step=self.step, **fields)

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
        atomic_write(path, text)  # уникальный tmp: два писателя одного файла не портят его друг другу
        return path


StepFn = Callable[[StepContext], None]


class Studio:
    def __init__(
        self, root: Path, steps: dict[str, StepFn], services: object = None, journal: events.EventLog | None = None
    ) -> None:
        self.root = root
        self.steps = steps
        self.services = services  # то, чем шаги пользуются: вкладки, LLM, рендер — см. steps.Services
        self.events = journal or events.EventLog(None)  # журнал событий; без него — не пишется
        # Запуск закончился: (проект, статусы шагов на старте) — для уведомлений; зовётся вне замка.
        self.on_stop: Callable[[dict, dict[str, str]], None] | None = None
        # Перед удалением выпуска уборкой (проект, каталог): сохранить то, что нужно аналитике.
        self.before_delete: Callable[[dict, Path], None] | None = None
        self._before: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()
        self._projects: dict[str, dict] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._cancels: dict[str, threading.Event] = {}
        # Шаги, которые выполнялись, когда сервис остановился: (проект, шаг) — см. resume_interrupted.
        self.interrupted: list[tuple[str, str]] = []
        self._closing = False  # close(): отмена от остановки сервиса, а не от человека
        root.mkdir(parents=True, exist_ok=True)
        self._load()

    # --- хранение ---

    def project_dir(self, project_id: str) -> Path:
        return self.root / project_id

    def _save(self, project: dict) -> None:
        folder = self.project_dir(project["id"])
        folder.mkdir(parents=True, exist_ok=True)
        project["updated_at"] = time.time()
        atomic_write(folder / "project.json", json.dumps(project, ensure_ascii=False, indent=2))

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
            for step_id, state in project["steps"].items():
                if state["status"] == RUNNING:
                    state.update(status=FAILED, error=INTERRUPTED, progress=None)
                    self.interrupted.append((project["id"], step_id))
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

    def list(self, scenario: str | None = None, *, archived: bool = False) -> list[dict]:
        """Выпуски, новые сверху; архивные — только с archived=True (и только они)."""
        with self._lock:
            items = [
                p
                for p in self._projects.values()
                if scenario in (None, p["scenario"]) and bool(p.get("archived")) == archived
            ]
            items.sort(key=lambda p: p["created_at"], reverse=True)
            return [
                {
                    "id": p["id"],
                    "scenario": p["scenario"],
                    "title": p["title"],
                    "created_at": p["created_at"],
                    "updated_at": p.get("updated_at") or p["created_at"],
                    "running": p["running"],
                    "current": LABELS.get(p.get("current") or ""),
                    "status": summary(p),
                    "attention": attention(p),
                    "archived": bool(p.get("archived")),
                    "source": p.get("source"),
                }
                for p in items
            ]

    def delete(self, project_id: str) -> int:
        """Уборка: удалить выпуск из архива целиком. Возвращает, сколько байт освободилось."""
        with self._lock:
            project = self._require(project_id)
            if project["running"] or not project.get("archived"):
                return 0
            if self.before_delete is not None:  # сбой здесь — выпуск не удаляется: история дороже места
                self.before_delete(copy.deepcopy(project), self.project_dir(project_id))
            del self._projects[project_id]
        self._emit(project, "project.deleted", actor=events.actor())
        return cleanup.remove_tree(self.project_dir(project_id))

    def archive(self, project_id: str, archived: bool) -> dict:
        """Убрать выпуск с доски и из списков (файлы остаются) или вернуть его."""

        def apply(project: dict) -> set[str]:
            project["archived"] = archived
            if archived:
                project["auto"] = False  # архивный выпуск сам не продолжается
            self._emit(project, "project.archived", actor=events.actor(), props={"archived": archived})
            return set()

        return self.update(project_id, apply)

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
            self._emit(project, "project.created", actor=events.actor())
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

    def mark_done(self, project_id: str, step_id: str, note: str | None = None, *, resume: bool = False) -> dict:
        """Результат шага дал человек (правка, загрузка): шаг готов, зависящие — заново.

        Можно и во время запуска, если ни шаг, ни зависящие от него сейчас не выполняются.
        Запущенный выпуск подхватит зависящие сам. resume — файл, которого выпуск ждал:
        остановившийся на нём выпуск продолжится; правка текста так не делает — человек
        может править дальше, а перегенерация дорогая."""
        with self._lock:
            project = self._require(project_id)
            self._check_idle(project, step_id)
            self._reset(project, step_id, include_self=False)
            state = project["steps"][step_id]
            status_from, waited = state["status"], _end_wait(state)
            state.update(status=DONE, error=None, note=note, progress=None, updated_at=time.time())
            self._save(project)
            self._emit(
                project,
                "step.human_input",
                actor=events.actor(),
                step=step_id,
                status_from=status_from,
                status_to=DONE,
                duration_s=waited,
                props={"note": note} if note else None,
            )
            if resume:
                self._resume(project)
            return copy.deepcopy(project)

    def note(self, project_id: str, step_id: str, note: str) -> dict:
        """Пометка у шага без смены статуса (например, «загружено 2 из 4»)."""
        with self._lock:
            project = self._require(project_id)
            self._check_idle(project, step_id)
            project["steps"][step_id]["note"] = note
            self._save(project)
            return copy.deepcopy(project)

    @contextlib.contextmanager
    def idle(self, project_id: str, step_id: str | None = None) -> Iterator[None]:
        """Запись файла выпуска под замком студии: ни выпуск (или шаг step_id и зависящие от него)
        не стартует между проверкой «не выполняется» и записью. Внутри — только файлы, без вызовов студии."""
        with self._lock:
            project = self._require(project_id)
            if step_id is None:
                if project["running"]:
                    raise Conflict("Проект сейчас выполняется — дождись или останови его.")
            else:
                self._check_idle(project, step_id)
            yield

    def check_idle(self, project_id: str, step_id: str) -> None:
        with self._lock:
            self._check_idle(self._require(project_id), step_id)

    def approve(self, project_id: str, step_id: str) -> dict:
        with self._lock:
            project = self._require(project_id)
            state = project["steps"][step_id]
            if state["status"] != WAITING:
                raise Conflict("Этот шаг ничего не ждёт.")
            state["approved"] = True
            state["poke"] = True
            project["auto"] = True
            waited = _end_wait(state)
            self._save(project)
            self._emit(
                project, "step.approved", actor=events.actor(), step=step_id, status_from=WAITING, duration_s=waited
            )
            self._resume(project)
            return copy.deepcopy(project)

    def run(self, project_id: str, only: str | None = None, state: dict | None = None) -> dict:
        """Запустить в фоне: все шаги, для которых всё готово, или один шаг заново (only).
        state — что положить в состояние шага only перед запуском (например, approved)."""
        with self._lock:
            project = self._require(project_id)
            if project["running"]:
                raise Conflict("Проект уже выполняется.")
            if only is not None:
                prev = project["steps"][only]
                status_from, waited, attempt = prev["status"], _end_wait(prev), prev.get("attempt", 0)
                self._reset(project, only, include_self=True)
                # Шаг перезапустили руками — нужен новый вариант, а не готовый из кэша.
                project["steps"][only].update({"fresh": True} | (state or {}))
                if attempt:
                    self._emit(
                        project,
                        "step.rerun",
                        actor=events.actor(),
                        step=only,
                        status_from=status_from,
                        attempt=attempt + 1,
                        duration_s=waited,
                    )
            else:
                project["auto"] = True
                # «Запустить всё» ещё раз пробует шаги, которые ждали человека или упали.
                for step_id, step_state in project["steps"].items():
                    if step_state["status"] in (WAITING, FAILED):
                        step_state["poke"] = True
                        waited = _end_wait(step_state)
                        if waited is not None:
                            self._emit(
                                project,
                                "step.human_input",
                                actor=events.actor(),
                                step=step_id,
                                status_from=WAITING,
                                duration_s=waited,
                                props={"action": "run_all"},
                            )
            self._start(project, only)
            return copy.deepcopy(project)

    def resume_interrupted(self, project_id: str, step_id: str) -> bool:
        """После рестарта: один раз продолжить шаг, прерванный остановкой сервиса.

        Только у выпуска, который шёл сам (auto), не в архиве и не шаг публикации; флаг
        resumed в состоянии шага не даёт зациклиться, если сервис падает на этом шаге снова.
        True — выпуск запущен."""
        with self._lock:
            project = self._projects.get(project_id)
            if project is None or project["running"] or project.get("archived") or not project.get("auto"):
                return False
            state = project["steps"][step_id]
            if step_id in NO_AUTO_RESUME or state.get("resumed") or state["status"] != FAILED:
                return False
            state.update(poke=True, resumed=True)
            self._start(project, None)
            return True

    def cancel(self, project_id: str) -> dict:
        with self._lock:
            project = self._require(project_id)
            project["auto"] = False  # остановленный выпуск сам не продолжается
            event = self._cancels.get(project_id)
            if event:
                event.set()
            self._save(project)
            return copy.deepcopy(project)

    def join(self, project_id: str, timeout: float | None = None) -> None:
        thread = self._threads.get(project_id)
        if thread:
            thread.join(timeout)

    def close(self) -> None:
        with self._lock:
            self._closing = True
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
            raise ProjectNotFound(project_id)
        return project

    def _check_idle(self, project: dict, step_id: str) -> None:
        """Трогать шаг можно, если ни он, ни зависящие от него сейчас не выполняются."""
        busy = [s for s in [step_id, *dependents(step_id)] if project["steps"][s]["status"] == RUNNING]
        if busy:
            raise Conflict(f"Сейчас выполняется «{LABELS[busy[0]]}» — дождись или останови выпуск.")

    def _start(self, project: dict, only: str | None) -> None:
        """Под замком: поднять поток выпуска."""
        project_id = project["id"]
        project["running"] = True
        self._before[project_id] = {s: st["status"] for s, st in project["steps"].items()}
        self._save(project)
        cancel = threading.Event()
        self._cancels[project_id] = cancel
        thread = threading.Thread(
            target=self._run, args=(project_id, only, cancel), name=f"studio-{project_id}", daemon=True
        )
        self._threads[project_id] = thread
        thread.start()

    def _resume(self, project: dict) -> None:
        """Под замком: человек что-то дал — продолжить запущенный ранее выпуск.
        Идущий запуск подхватит шаги сам, остановленный кнопкой — не трогаем."""
        if not project["running"] and project.get("auto") and self._next(project) is not None:
            self._start(project, None)

    @staticmethod
    def _next(project: dict) -> Step | None:
        """Первый шаг, который можно выполнить: всё, от чего он зависит, готово, а сам он
        не начат — или ждал/упал и его попросили попробовать снова (poke)."""
        for step in STEPS:
            state = project["steps"][step.id]
            wanted = state["status"] == PENDING or (state["status"] in (WAITING, FAILED) and state.get("poke"))
            if wanted and not blockers(project, step.id):
                return step
        return None

    def _reset(self, project: dict, step_id: str, *, include_self: bool) -> None:
        targets = ([step_id] if include_self else []) + dependents(step_id)
        for target in targets:
            state = project["steps"][target]
            # Номер попытки и начало ожидания переживают сброс: по ним журнал считает перезапуски
            # и время, которое шаг ждал человека.
            keep = {k: state[k] for k in ("job", "keep", "attempt", "waiting_since") if k in state}
            project["steps"][target] = _fresh_state() | keep

    def _log(self, project_id: str, message: str) -> None:
        with self._lock:
            project = self._projects[project_id]
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
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

    def annotate(self, project_id: str, **fields) -> None:
        """Поля проекта вне шагов (например, ход выжимки) — можно и во время запуска."""
        with self._lock:
            project = self._require(project_id)
            project.update(fields)
            self._save(project)

    def log(self, project_id: str, message: str) -> None:
        with self._lock:
            self._require(project_id)
        self._log(project_id, message)
        with self._lock:
            self._save(self._projects[project_id])

    def _set_title(self, project_id: str, title: str) -> None:
        with self._lock:
            project = self._projects[project_id]
            project["title"] = title
            self._save(project)

    def _emit(self, project: dict, event: str, *, actor: str = events.SYSTEM, **fields) -> None:
        """Событие выпуска в журнал; сбой записи журнал глотает сам."""
        self.events.emit(event, actor=actor, project_id=project["id"], scenario=project.get("scenario"), **fields)

    def _set(self, project_id: str, step_id: str, **values) -> dict:
        with self._lock:
            project = self._projects[project_id]
            project["steps"][step_id].update(values, updated_at=time.time())
            if values.get("status") == RUNNING:
                project["current"] = step_id
            self._save(project)
            return copy.deepcopy(project)

    def _run(self, project_id: str, only: str | None, cancel: threading.Event) -> None:
        step: Step | None = STEP_BY_ID[only] if only is not None else None
        try:
            while not cancel.is_set() and not self._shutting_down():
                if only is None:
                    # Выбор шага и решение «больше нечего» — под замком: загрузка человека,
                    # пришедшая в этот момент, либо попадёт в выбор, либо поднимет новый запуск.
                    with self._lock:
                        project = self._projects[project_id]
                        step = self._next(project)
                        if step is None:
                            self._stop(project)
                            return
                        project["steps"][step.id].pop("poke", None)
                self._run_step(project_id, step, cancel)
                if only is not None:
                    break
        finally:
            with self._lock:
                project = self._projects[project_id]
                if project["running"]:
                    self._stop(project)
                done = copy.deepcopy(project)
                before = self._before.pop(project_id, {})
            if self.on_stop is not None and not done["running"]:
                with contextlib.suppress(Exception):  # уведомление не должно ронять поток выпуска
                    self.on_stop(done, before)

    def _stop(self, project: dict) -> None:
        project["running"] = False
        project["current"] = None
        self._cancels.pop(project["id"], None)
        self._save(project)

    def _run_step(self, project_id: str, step: Step, cancel: threading.Event) -> bool:
        """Шаг с записью в лог процесса: старт и итог с длительностью."""
        started = time.monotonic()
        log_event("step.start", project=project_id, step=step.id)
        try:
            return self._run_step_body(project_id, step, cancel)
        finally:
            with self._lock:
                current = self._projects[project_id]["steps"][step.id]
                if current["status"] == DONE:  # шаг дошёл до конца — следующий рестарт снова можно продолжить
                    current.pop("resumed", None)
                state = dict(current)
            log_event(
                f"step.{state['status']}",
                level=logging.WARNING if state["status"] == FAILED else logging.INFO,
                project=project_id,
                step=step.id,
                duration_s=round(time.monotonic() - started, 1),
                error=state.get("error") if state["status"] == FAILED else None,
            )

    def _shutting_down(self) -> bool:
        return self._closing or runner.stopping()

    def _run_step_body(self, project_id: str, step: Step, cancel: threading.Event) -> bool:
        """True — можно идти дальше."""
        with self._lock:
            state = self._projects[project_id]["steps"][step.id]
            status_from, waited = state["status"], _end_wait(state)
            attempt = state.get("attempt", 0) + 1
        started = time.time()
        project = self._set(
            project_id,
            step.id,
            status=RUNNING,
            error=None,
            note=None,
            progress=None,
            started_at=started,
            attempt=attempt,
        )
        self._emit(
            project,
            "step.started",
            step=step.id,
            status_from=status_from,
            status_to=RUNNING,
            attempt=attempt,
            props={"waited_s": round(waited, 3)} if waited is not None else None,
        )
        ctx = StepContext(self, project, step.id, cancel)
        self._log(project_id, f"▶ {step.label}")

        def finish(event: str, status: str, error_class: str | None = None, **values) -> None:
            done = self._set(project_id, step.id, status=status, progress=None, **values)
            self._emit(
                done,
                event,
                step=step.id,
                status_from=RUNNING,
                status_to=status,
                attempt=attempt,
                duration_s=time.time() - started,
                error_class=error_class,
            )

        try:
            self.steps[step.id](ctx)
        except Waiting as exc:
            finish("step.waiting", WAITING, note=str(exc), waiting_since=time.time())
            self._log(project_id, f"⏸ {step.label}: {exc}")
            return False
        except Skip as exc:
            finish("step.skipped", SKIPPED, note=str(exc))
            self._log(project_id, f"↷ {step.label}: {exc}")
            return True
        except Exception as exc:  # noqa: BLE001 — сбой шага не должен уронить поток проекта
            if self._shutting_down():
                # Остановка сервиса (SIGTERM): шаг остаётся «выполняется» — после рестарта
                # он будет прерванным (уведомление и, для auto, продолжение), а не сломанным.
                self._log(project_id, f"■ {step.label}: сервис останавливается")
                return False
            if isinstance(exc, runner.Canceled):
                finish("step.canceled", PENDING, note="Остановлено.")
                self._log(project_id, f"■ {step.label}: остановлено")
            elif isinstance(exc, StudioError):
                finish("step.failed", FAILED, type(exc).__name__, error=str(exc))
                self._log(project_id, f"✗ {step.label}: {exc}")
            else:
                finish("step.failed", FAILED, type(exc).__name__, error=f"Внутренняя ошибка: {exc!r}")
                self._log(project_id, f"✗ {step.label}: {exc!r}")
            return False
        if cancel.is_set():
            if self._shutting_down():  # как выше: шаг прерван остановкой сервиса
                return False
            finish("step.canceled", PENDING, note="Остановлено.")
            return False
        finish("step.finished", DONE)
        self._log(project_id, f"✓ {step.label}")
        return True


def _fresh_state() -> dict:
    return {"status": PENDING, "error": None, "note": None, "progress": None, "updated_at": None}


def _end_wait(state: dict) -> float | None:
    """Закрыть ожидание человека: убрать waiting_since из состояния, вернуть, сколько секунд ждали."""
    since = state.pop("waiting_since", None)
    return max(0.0, time.time() - since) if isinstance(since, (int, float)) else None


def attention(project: dict) -> dict | None:
    """Первый шаг, которому нужен человек: ждёт или упал. None — ничего не нужно."""
    if project.get("running"):
        return None
    for step in STEPS:
        state = project["steps"][step.id]
        if state["status"] in (WAITING, FAILED):
            text = state.get("error") if state["status"] == FAILED else state.get("note")
            return {"step": step.id, "label": step.label, "status": state["status"], "text": text or ""}
    return None


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
