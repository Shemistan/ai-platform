"""Оркестрация ревью (ITERATIVE_REVIEW_SPEC §9): запуски, управление, версии — поверх MontageProjects.

`Review` живёт в `MontageProjects.review`: создаёт запуск (baseline V<n> с текущего ролика), отдаёт
его UI, ставит на паузу, продолжает, останавливает; раунды исполняет воркер проекта действием
`review` (см. `run_round`). Один незавершённый запуск на проект. Проект, изменённый вне запуска
(план, раскадровка, пересборка), делает запуск устаревшим — `stale_revision` (optimistic locking).
"""

from __future__ import annotations

import threading
from pathlib import Path

from aiplatform.montage import decisions
from aiplatform.montage.review import policy as policy_module
from aiplatform.montage.review import state, versions

STALE = "Проект изменён вне запуска ревью (план, раскадровка или пересборка) — этот запуск устарел, начни новый."


class ReviewError(ValueError):
    """Неверный запрос (400)."""


class ReviewConflict(ValueError):
    """Не к месту (409): проект занят, запуск не в том состоянии, ревизия устарела."""


class Review:
    def __init__(self, projects) -> None:
        self.projects = projects  # MontageProjects: lock, dir, _enqueue, cancel events
        self.config = projects.config

    # ---------- чтение ----------

    def folder(self, pid: str) -> Path:
        return self.projects.dir(pid)

    def runs(self, pid: str) -> list[dict] | None:
        if self.projects.get(pid) is None:
            return None
        return [state.public(r) for r in state.list_runs(self.folder(pid))]

    def get(self, pid: str, run_id: str) -> dict | None:
        if self.projects.get(pid) is None:
            return None
        run = state.load_run(self.folder(pid), run_id)
        return state.public(run) if run else None

    def _run(self, pid: str, run_id: str) -> dict:
        run = state.load_run(self.folder(pid), run_id)
        if run is None:
            raise ReviewConflict("Такого запуска нет.")
        return run

    def open_run(self, pid: str) -> dict | None:
        """Незавершённый запуск проекта (один на проект)."""
        return next((r for r in state.list_runs(self.folder(pid)) if r["state"] not in state.FINAL), None)

    def versions(self, pid: str) -> list[dict] | None:
        if self.projects.get(pid) is None:
            return None
        return versions.listing(self.folder(pid))

    def version_file(self, pid: str, label: str, name: str) -> Path | None:
        n = versions.number(label)
        if n is None or self.projects.get(pid) is None:
            return None
        return versions.file(self.folder(pid), n, versions.OUTPUT if name == "video" else name)

    def capabilities(self) -> dict:
        """Чем располагает запуск: без мультимодальной модели роли «глаз» деградируют (§1.4)."""
        return {
            "llm": "available" if self.config.llm_available() else "unavailable",
            "hyperframes": "available" if self.config.hyperframes_available() else "unavailable",
            "vision": "unavailable",  # локальной VLM на машине нет (ENVIRONMENT_AUDIT, ADR 0012)
        }

    # ---------- создание и управление ----------

    def create(self, pid: str, payload: object) -> dict | None:
        data = payload if isinstance(payload, dict) else {}
        try:
            policy = policy_module.clean(data.get("policy", data))
        except policy_module.PolicyError as exc:
            raise ReviewError(str(exc)) from exc
        with self.projects._lock:
            project = self.projects._projects.get(pid)
            if project is None:
                return None
            self.projects._idle(project)
            folder = self.folder(pid)
            if not (folder / "output.mp4").is_file() or not (folder / "qa_report.json").is_file():
                raise ReviewConflict("Ревью начинается с готового ролика: сначала собери его (QA-отчёт обязателен).")
            if self.open_run(pid) is not None:
                raise ReviewConflict("Запуск ревью уже идёт — останови его или дождись конца.")
            base = self._baseline(folder)
            lock = {"project_updated_at": project["updated_at"], "revision": len(project.get("history") or []),
                    "manifests_hash": self.manifests_hash(folder)}  # fmt: skip
            run = state.new_run(pid, policy, lock=lock, capabilities=self.capabilities())
            run["versions"] = [base["label"]]
            run["accepted_version"] = base["label"]
            state.checkpoint(folder, run)
            decisions.record(folder, "human", "review:create", output=policy, ids=[run["run_id"]],
                             rationale=f"ревью до {policy['max_rounds']} раундов, режим {policy['mode']}")  # fmt: skip
            self.projects._history(project, "review", f"запуск {run['run_id']}: baseline {base['label']}")
            self.projects._touch(project)
            return state.public(run)

    def _baseline(self, folder: Path) -> dict:
        """V0 (или следующая версия, если ролик пересобран после прошлого запуска): неизменяемый baseline."""
        known = versions.listing(folder)
        current = decisions.file_sha(folder / "output.mp4")
        for record in reversed(known):
            if record["output"]["sha256"] == current and versions.integrity(folder, record["n"]) is None:
                return record
        n = (known[-1]["n"] + 1) if known else 0
        return versions.save(folder, n, round_n=0, parent=known[-1]["label"] if known else None, accepted=True,
                             ffmpeg=self.config.ffmpeg, reason="baseline")  # fmt: skip

    def control(self, pid: str, run_id: str, verb: str) -> dict | None:
        """start | pause | resume | stop — только из допустимых состояний (§9)."""
        if verb not in ("pause", "resume", "stop", "start"):
            raise ReviewError("Действие — start, pause, resume или stop.")
        enqueue = False
        with self.projects._lock:
            project = self.projects._projects.get(pid)
            if project is None:
                return None
            run = self._run(pid, run_id)
            folder = self.folder(pid)
            if verb == "start":
                if run["state"] != "idle":
                    raise ReviewConflict("Запуск уже стартовал.")
                self.check_lock(run, raise_conflict=True)
                self.projects._idle(project)
                state.advance(run, "preparing", reason="start")
                state.checkpoint(folder, run)
                enqueue = True
            elif verb == "pause":
                if run["state"] not in (*state.ACTIVE, *state.WAITING, "accepted", "rejected"):
                    raise ReviewConflict(f"Пауза невозможна из состояния «{run['state']}».")
                run["blocking_reason"] = "пауза по просьбе человека"
                state.advance(run, "paused", reason="user")
                state.checkpoint(folder, run)
                self.projects._cancel_quiet(pid)
            elif verb == "resume":
                if run["state"] != "paused":
                    raise ReviewConflict("Продолжить можно только запуск на паузе.")
                self.check_lock(run, raise_conflict=True)
                self.projects._idle(project)
                back = run["checkpoint"]["state"]
                if back in state.FINAL:
                    raise ReviewConflict("Запуск уже завершён.")
                state.advance(run, back, reason="resume", blocking_reason=None)
                state.checkpoint(folder, run)
                enqueue = back not in state.WAITING
            else:  # stop
                if run["state"] in state.FINAL:
                    raise ReviewConflict("Запуск уже завершён.")
                if run["state"] in state.ACTIVE:
                    run["stop_reason"] = "user"  # воркер увидит и закончит запуск после отмены стадии
                    state.checkpoint(folder, run)
                    self.projects._cancel_quiet(pid)
                else:
                    target = "completed" if run["rounds"] else "cancelled"
                    state.advance(run, target, reason="user", stop_reason="user", blocking_reason=None)
                    state.checkpoint(folder, run)
                    decisions.record(folder, "human", "review:stop", ids=[run_id], rationale="остановлено человеком")
        if enqueue:
            self.projects._enqueue(pid, "review", run_id)
        return self.get(pid, run_id)

    def manifests_hash(self, folder: Path) -> str:
        """Отпечаток того, что ревью считает «своей» ревизией проекта: план, раскадровка, таймлайн, ролик."""
        parts = {}
        for name in ("plan.json", "storyboard.json", "timeline.json"):
            path = folder / name
            parts[name] = decisions.file_sha(path) if path.is_file() else None
        output = folder / "output.mp4"
        parts["output"] = decisions.file_sha(output) if output.is_file() else None
        return decisions.sha(parts)[:16]

    def check_lock(self, run: dict, *, raise_conflict: bool = False) -> bool:
        """Проект изменён вне запуска? Да → запуск blocked (stale_revision); raise_conflict — и 409."""
        folder = self.folder(run["project_id"])
        if self.manifests_hash(folder) == run["lock"].get("manifests_hash"):
            return True
        run["blocking_reason"] = STALE
        if state.can(run, "blocked"):
            state.advance(run, "blocked", reason="stale_revision")
            state.checkpoint(folder, run)
        if raise_conflict:
            raise ReviewConflict(STALE)
        return False

    def relock(self, run: dict) -> None:
        """После своей правки или пересборки — запуск снова владеет текущей ревизией."""
        folder = self.folder(run["project_id"])
        run["lock"] = {**run["lock"], "manifests_hash": self.manifests_hash(folder)}

    def cancel_event(self, pid: str) -> threading.Event | None:
        return self.projects._cancels.get(pid)

    def on_load(self, pid: str) -> list[str]:
        """Старт сервиса: активные запуски проекта → paused с причиной (§9, resume без пересчёта)."""
        return [r["run_id"] for r in state.interrupted(self.folder(pid))]

    # ---------- воркер ----------

    def run(self, project: dict, run_id: str, cancel: threading.Event) -> str:
        """Действие `review` воркера: крутить стадии запуска, пока не ожидание человека, пауза или конец.

        Возвращает подпись состояния проекта для карточки. Раунды — в `rounds.py` (следующая подзадача):
        здесь только каркас, чтобы запуск честно доходил до blocked, пока стадий нет."""
        from aiplatform.montage.review import rounds

        return rounds.drive(self, project, run_id, cancel)

    def after_cancel(self, run_id: str) -> str:
        """Подпись карточки после прерывания действия ревью: пауза или остановка человеком."""
        return "Ревью остановлено" if run_id else "Готово"
