"""Оркестрация ревью (ITERATIVE_REVIEW_SPEC §9): запуски, управление, версии — поверх MontageProjects.

`Review` живёт в `MontageProjects.review`: создаёт запуск (baseline V<n> с текущего ролика), отдаёт
его UI, ставит на паузу, продолжает, останавливает; раунды исполняет воркер проекта действием
`review` (см. `run_round`). Один незавершённый запуск на проект. Проект, изменённый вне запуска
(план, раскадровка, пересборка), делает запуск устаревшим — `stale_revision` (optimistic locking).
"""

from __future__ import annotations

import re
import threading
import time
from pathlib import Path

from aiplatform.montage import decisions
from aiplatform.montage.review import issues as issues_module
from aiplatform.montage.review import planner, state, versions
from aiplatform.montage.review import policy as policy_module

STALE = "Проект изменён вне запуска ревью (план, раскадровка или пересборка) — этот запуск устарел, начни новый."
_BUNDLE_FILE = re.compile(r"^(frames/f\d{6}\.jpg|proxy\.mp4|bundle\.json)$")


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
        same = [
            r
            for r in reversed(known)
            if r["output"]["sha256"] == current and versions.integrity(folder, r["n"]) is None
        ]
        accepted = [r for r in same if r["accepted"]]
        if accepted or same:  # принятая версия с тем же файлом важнее отклонённого кандидата-близнеца
            return (accepted or same)[0]
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

    # ---------- раунд: замечания, решения, план, сравнение (§10) ----------

    def _round(self, pid: str, run_id: str, n: str) -> tuple[dict, dict]:
        if self.projects.get(pid) is None:
            raise ReviewConflict("Проект не найден.")
        run = self._run(pid, run_id)
        number = int(n) if str(n).isdigit() else 0
        rnd = state.load_round(self.folder(pid), run_id, number)
        if rnd is None:
            raise ReviewConflict("Такого раунда нет.")
        return run, rnd

    def issues(self, pid: str, run_id: str, n: str) -> dict:
        run, rnd = self._round(pid, run_id, n)
        data = state.read_json(self.folder(pid), run_id, rnd["n"], "issues.json") or {"issues": []}
        reports = []
        for role_id in run["policy"]["reviewer_roles"]:
            report = state.read_json(self.folder(pid), run_id, rnd["n"], f"reports/{role_id}.json")
            if report:
                keys = ("role", "kind", "status", "degraded", "manual_check", "elapsed_s", "evidence_types", "error",
                        "measurements", "degraded_reason")  # fmt: skip
                reports.append({k: report.get(k) for k in keys} | {"issues": len(report["issues"])})
        return {"round": rnd, "issues": data["issues"], "reports": reports,
                "counts": issues_module.counts(data["issues"])}  # fmt: skip

    def repair_plan(self, pid: str, run_id: str, n: str) -> dict:
        _run, rnd = self._round(pid, run_id, n)
        return state.read_json(self.folder(pid), run_id, rnd["n"], "repair_plan.json") or {
            "actions": [],
            "round_id": rnd["round_id"],
        }

    def comparison(self, pid: str, run_id: str, n: str) -> dict:
        _run, rnd = self._round(pid, run_id, n)
        data = state.read_json(self.folder(pid), run_id, rnd["n"], "comparison.json")
        if data is None:
            return {"round_id": rnd["round_id"], "decision": None, "pending": True}
        if rnd.get("candidate_version"):
            data["diff"] = versions.diff(self.folder(pid), versions.number(rnd["base_version"]),
                                         versions.number(rnd["candidate_version"]))  # fmt: skip
        return data

    def decisions(self, pid: str, run_id: str, n: str, payload: object) -> dict:
        """Решения человека по замечаниям, locked сцены, утверждение плана (§2, §6.6)."""
        data = payload if isinstance(payload, dict) else {}
        raw = data.get("issues") or data.get("decisions") or []
        if not isinstance(raw, list):
            raise ReviewError("issues — список {issue_id, status, comment?}.")
        chosen: dict[str, str] = {}
        comments: dict[str, str] = {}
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("issue_id"), str):
                raise ReviewError("issues — список {issue_id, status, comment?}.")
            if item.get("status") not in ("accepted", "rejected", "deferred"):
                raise ReviewError("status — accepted, rejected или deferred.")
            chosen[item["issue_id"]] = item["status"]
            if isinstance(item.get("comment"), str) and item["comment"].strip():
                comments[item["issue_id"]] = item["comment"].strip()[:500]
        approve_all = bool(data.get("approve_plan"))
        locked = data.get("locked_scenes")
        if locked is not None and (not isinstance(locked, list) or not all(isinstance(s, str) for s in locked)):
            raise ReviewError("locked_scenes — список id сцен.")
        enqueue = False
        with self.projects._lock:
            project = self.projects._projects[pid]
            run, rnd = self._round(pid, run_id, n)
            folder = self.folder(pid)
            if run["state"] != "awaiting_approval" or rnd["n"] != run["round"]:
                raise ReviewConflict("Запуск сейчас не ждёт утверждения плана.")
            issues = (state.read_json(folder, run_id, rnd["n"], "issues.json") or {}).get("issues") or []
            plan_data = state.read_json(folder, run_id, rnd["n"], "repair_plan.json") or {"actions": []}
            if locked is not None:
                run["policy"]["locked_scenes"] = list(dict.fromkeys([*run["policy"]["locked_scenes"], *locked]))
                for issue in issues:
                    if issue["change"].get("scene_id") in run["policy"]["locked_scenes"] and issue["status"] in (
                        "proposed",
                        "accepted",
                    ):
                        chosen.setdefault(issue["issue_id"], "rejected")
            for issue in issues:
                if issue["issue_id"] in comments:
                    issue["comments"].append(
                        {"at": round(time.time(), 3), "by": "human", "text": comments[issue["issue_id"]]}
                    )
            planner.approve(plan_data, issues, decisions=chosen, approve_all=approve_all)
            state.write_json(
                folder, run_id, rnd["n"], "issues.json", {"issues": issues, "made_at": round(time.time(), 3)}
            )
            state.write_json(folder, run_id, rnd["n"], "repair_plan.json", plan_data)
            rnd["counts"] = issues_module.counts(issues)
            decisions.record(folder, "human", "review:decisions", ids=list(chosen)[:50],
                             rationale=f"решений {len(chosen)}, план: {plan_data['approval']}, locked: {len(locked or [])}")  # fmt: skip
            approved = any(a["status"] == "approved" for a in plan_data["actions"])
            if plan_data["approval"] == "given" and approved:
                state.round_stage(rnd, "approve", "done", "утверждено человеком")
                state.save_round(folder, run, rnd)
                state.advance(run, "repairing", reason="approved", blocking_reason=None)
                state.checkpoint(folder, run)
                self.projects._idle(project)
                enqueue = True
            elif plan_data["approval"] == "denied":
                rnd["decision"], rnd["decision_reason"] = "no_actions", "человек отклонил все правки"
                rnd["finished_at"] = round(time.time(), 3)
                state.round_stage(rnd, "approve", "done", "отклонено")
                state.save_round(folder, run, rnd)
                state.advance(run, "completed", reason="user", stop_reason="no_approved_actions", blocking_reason=None)
                state.checkpoint(folder, run)
            else:
                state.save_round(folder, run, rnd)
                state.checkpoint(folder, run)
        if enqueue:
            self.projects._enqueue(pid, "review", run_id)
        return self.issues(pid, run_id, n)

    def execute(self, pid: str, run_id: str, n: str) -> dict:
        """Утвердить план целиком и исполнить (§10 `execute`)."""
        return self.decisions(pid, run_id, n, {"approve_plan": True})

    def decide(self, pid: str, run_id: str, n: str, payload: object) -> dict:
        """Спорное сравнение: человек принимает кандидата или откатывает (§8)."""
        data = payload if isinstance(payload, dict) else {}
        choice = data.get("decision")
        if choice not in ("accept", "reject"):
            raise ReviewError("decision — accept или reject.")
        reason = str(data.get("reason") or "").strip()[:300]
        with self.projects._lock:
            project = self.projects._projects[pid]
            run, rnd = self._round(pid, run_id, n)
            if run["state"] != "awaiting_decision" or rnd["n"] != run["round"]:
                raise ReviewConflict("Запуск сейчас не ждёт решения по версии.")
            self.projects._idle(project)
            from aiplatform.montage.review import rounds

            ctx = rounds.Round(self, project, run, threading.Event())
            decision = "accepted" if choice == "accept" else "rejected"
            comparison = ctx.read_round("comparison.json")
            comparison.update(decision=decision, decided_by="human", reason=reason or f"решение человека: {decision}")
            ctx.write_round("comparison.json", comparison)
            ctx.settle(decision, comparison["reason"], "human")
            state.checkpoint(self.folder(pid), run)
        self.projects._enqueue(pid, "review", run_id)
        return self.get(pid, run_id)

    def select_revision(self, pid: str, run_id: str, payload: object) -> dict | None:
        """Вернуть в проект любую версию запуска (V0..Vn): манифесты и ролик копией, запуск запоминает выбор."""
        data = payload if isinstance(payload, dict) else {}
        n = versions.number(data.get("version"))
        if n is None:
            raise ReviewError("version — метка вида V2.")
        with self.projects._lock:
            project = self.projects._projects.get(pid)
            if project is None:
                return None
            self.projects._idle(project)
            run = self._run(pid, run_id)
            if run["state"] in state.ACTIVE:
                raise ReviewConflict("Запуск ещё работает — дождись конца раунда или поставь на паузу.")
            folder = self.folder(pid)
            if versions.get(folder, n) is None:
                raise ReviewConflict("Такой версии нет.")
            problem = versions.integrity(folder, n)
            if problem:
                raise ReviewConflict(f"{versions.label(n)}: {problem}.")
            versions.restore(folder, n)
            board = (self.projects._read_json(pid, "storyboard.json") or {}).get("scenes") or []
            from aiplatform.montage import storyboard

            project["storyboard"] = storyboard.summary(board) if board else None
            project["timeline"] = None
            timeline = self.projects._read_json(pid, "timeline.json")
            if timeline:
                from aiplatform.montage import timeline as timeline_module

                project["timeline"] = timeline_module.summary(timeline)
            report = self.projects._read_json(pid, "qa_report.json") or {}
            if report:
                project["qa"] = {"gates": report.get("gates"), "counts": report.get("counts"),
                                 "attempts": report.get("repair_attempts", 0)}  # fmt: skip
            run["selected_version"] = versions.label(n)
            self.relock(run)
            state.checkpoint(folder, run)
            decisions.record(folder, "human", "review:select_revision", ids=[run_id, versions.label(n)],
                             rationale=f"в проект возвращена версия {versions.label(n)}")  # fmt: skip
            self.projects._history(project, "review", f"выбрана версия {versions.label(n)}")
            self.projects._touch(project)
            self.projects._snapshot(project, f"review select {versions.label(n)}")
            return state.public(run)

    def bundle_file(self, pid: str, run_id: str, n: str, name: str) -> Path | None:
        """Файл бандла раунда (кадр-улика или прокси) по базовой версии раунда."""
        if self.projects.get(pid) is None or not _BUNDLE_FILE.match(name):
            return None
        rnd = state.load_round(self.folder(pid), run_id, int(n) if str(n).isdigit() else 0)
        if rnd is None:
            return None
        from aiplatform.montage.review import rounds

        path = rounds.bundle_dir(self.folder(pid), rnd["base_version"]) / name
        return path if path.is_file() else None
