"""Стадии раунда ревью (ITERATIVE_REVIEW_SPEC §9): воркер зовёт `drive` действием `review`.

`drive` читает запуск с чекпоинта и ведёт его: preparing (раунд, бандл) → reviewing (роли) →
consolidating (замечания, план; ворота) → repairing (правки манифестов) → rendering (пересборка) →
validating (кандидат V<n+1>, повторные замеры) → comparing (regression gate, судья) → accepted | rejected
→ следующий раунд или completed. Каждая стадия идемпотентна (артефакт есть → пропуск) и пишет чекпоинт;
пауза (cancel) прерывает стадию, запуск остаётся на последнем чекпоинте. Ожидание человека
(awaiting_approval, awaiting_decision) — выход из цикла; продолжение — через API (`engine.py`).
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from aiplatform.core import runner
from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import decisions, render, storyboard
from aiplatform.montage import style as style_module
from aiplatform.montage.review import bundle, compare, planner, repair, reviewers, state, versions
from aiplatform.montage.review import issues as issues_module
from aiplatform.montage.review import policy as policy_module
from aiplatform.studio import llm

BUNDLES = "review/bundles"
REPEAT_SHARE = 0.8
LABELS = {
    "awaiting_approval": "Ревью ждёт утверждения плана",
    "awaiting_decision": "Ревью ждёт решения по версии",
    "paused": "Ревью на паузе",
    "blocked": "Ревью заблокировано",
    "completed": "Ревью завершено",
    "cancelled": "Ревью остановлено",
    "failed": "Ревью упало",
}


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def bundle_dir(folder: Path, label: str) -> Path:
    return folder / BUNDLES / label


class Round:
    """Контекст одного прогона drive: запуск, раунд, проект, отмена."""

    def __init__(self, review, project: dict, run: dict, cancel: threading.Event) -> None:
        self.engine = review
        self.projects = review.projects
        self.config = review.config
        self.project = project
        self.run = run
        self.cancel = cancel
        self.folder = review.folder(project["id"])
        self.rnd: dict | None = state.load_round(self.folder, run["run_id"], run["round"]) if run["round"] else None

    # ---------- общее ----------

    def save(self) -> None:
        if self.rnd is not None:
            state.save_round(self.folder, self.run, self.rnd)
        state.checkpoint(self.folder, self.run)

    def go(self, new_state: str, *, reason: str = "", **fields) -> None:
        state.advance(self.run, new_state, reason=reason, **fields)
        self.save()
        if self.cancel.is_set():
            raise runner.Canceled

    def stage(self, stage_id: str, percent: float, detail: str = "") -> None:
        self.projects._stage(self.project, self.cancel, stage_id, percent, detail)

    def note(self, text: str) -> None:
        self.projects._note(self.project, text)

    def decide(self, role: str, action: str, **kw) -> None:
        decisions.record(self.folder, role, action, ids=[self.run["run_id"], *(kw.pop("ids", []))], **kw)

    def ask_for(self, role: str):
        """LLM для роли: запись в журнал решений, счётчик бюджета; None — LLM нет."""
        if not self.config.llm_available():
            return None

        def ask(prompt: str):
            data = self.projects._llm(prompt, self.project, self.cancel, role=role, ids=[self.run["run_id"]])
            self.run["budget"]["llm_calls"] += 1
            return data, " ".join(self.config.llm_cmd), decisions.sha(prompt.encode("utf-8"))[:12]

        return ask

    def base_n(self) -> int:
        return versions.number(self.rnd["base_version"]) if self.rnd else versions.number(self.run["accepted_version"])

    def rnd_path(self, name: str) -> Path:
        return state.round_dir(self.folder, self.run["run_id"], self.rnd["n"]) / name

    def read_round(self, name: str) -> dict:
        return _read(self.rnd_path(name))

    def write_round(self, name: str, data: dict) -> None:
        state.write_json(self.folder, self.run["run_id"], self.rnd["n"], name, data)

    def rnd_stage(self, stage_id: str, status: str, detail: str = "") -> None:
        state.round_stage(self.rnd, stage_id, status, detail)

    def event(self, type_: str, *, status: str = "running", message: str = "", **fields) -> None:
        """Событие раунда для панели прогресса (§14.5): run_id проекта, round_id раунда."""
        if self.rnd is not None:
            self.project["review_round"] = self.rnd["round_id"]
        self.projects.emit(self.project, type_, status=status, message=message, **fields)

    # ---------- стадии ----------

    def prepare(self) -> None:
        finished = self.rnd is not None and self.rnd.get("decision") is not None
        if self.rnd is None or finished:
            self.rnd = state.new_round(self.run, self.run["accepted_version"])
            self.run["round"] = self.rnd["n"]
            self.save()
            self.event("round_started", message=f"раунд {self.rnd['n']} от {self.rnd['base_version']}",
                       metrics={"max_rounds": self.run["policy"]["max_rounds"], "n": self.rnd["n"],
                                "stages": list(state.STAGES)})  # fmt: skip
        if self.cancel.is_set():
            raise runner.Canceled
        base = self.base_n()
        problem = versions.integrity(self.folder, base)
        if problem:
            raise runner.RunFailed(f"Baseline {self.rnd['base_version']}: {problem}.")
        self.rnd_stage("analyze", "running", "бандл")
        self.save()
        self.stage("review_bundle", 0, self.rnd["base_version"])
        self.ensure_bundle(base)
        self.stage("review_bundle", 100)
        self.go("reviewing", reason=f"round {self.rnd['n']}")

    def ensure_bundle(self, n: int) -> dict:
        label = versions.label(n)
        return bundle.build(
            self.config,
            versions.folder_of(self.folder, n),
            bundle_dir(self.folder, label),
            self.cancel,
            run_ffmpeg=self.projects._ffmpeg_run,
            pause_s=self.project["options"]["max_pause"] + 0.3,
            progress=lambda detail, pct: self.stage("review_bundle", pct, f"{label}: {detail}"),
        )

    def context(self, n: int, *, previous: list[dict] | None = None) -> reviewers.Context:
        vdir = versions.folder_of(self.folder, n)
        timeline = _read(vdir / "timeline.json")
        tracks = {t["id"]: t["clips"] for t in timeline.get("tracks") or []}
        fps = int(timeline.get("fps") or render.FPS)
        manifest = bundle.load(bundle_dir(self.folder, versions.label(n)))
        return reviewers.Context(
            round_id=self.rnd["round_id"], revision_id=versions.label(n), fps=fps,
            duration_frames=int(timeline.get("duration_frames") or 0),
            duration_s=(timeline.get("duration_frames") or 0) / fps,
            scenes=(_read(vdir / "storyboard.json")).get("scenes") or [],
            beats=(_read(vdir / "beats.json")).get("beats") or [],
            style=_read(vdir / "style_bible.json") or style_module.DEFAULT,
            captions=tracks.get("captions") or [], presenter=tracks.get("presenter") or [],
            qa_items=bundle.qa_items(vdir), bundle=manifest, policy=self.run["policy"],
            options=self.project["options"], capabilities=self.run["capabilities"], role="",
            texts=bundle.texts(vdir, self.folder, previous_issues=previous, fps=fps),
        )  # fmt: skip

    def review(self) -> None:
        base = self.base_n()
        previous = self.previous_issues()
        ctx = self.context(base, previous=previous)
        roles = self.run["policy"]["reviewer_roles"]
        self.rnd_stage("analyze", "running", "роли")
        reports_dir = self.rnd_path("reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        for k, role_id in enumerate(roles):
            path = reports_dir / f"{role_id}.json"
            label = reviewers.ROLES[role_id].label
            if path.is_file():
                continue
            self.stage("review_roles", 100 * k / len(roles), label)
            started = time.time()
            report = reviewers.run_role(role_id, ctx, self.ask_for(f"review_{role_id}"))
            atomic_write(path, json.dumps(report, ensure_ascii=False, indent=1))
            self.decide(f"review_{role_id}", "review:report", output=report["issues"],
                        rationale=f"{label}: замечаний {len(report['issues'])}, {report['status']}"
                        + (", без VLM — degraded" if report["degraded"] else ""))  # fmt: skip
            self.event("reviewer_finished", status=report["status"] == "done" and "completed" or "skipped",
                       agent_id=f"review_{role_id}", task_id=role_id, message=f"{label}: замечаний {len(report['issues'])}",
                       metrics={"issues": len(report["issues"]), "elapsed_s": report["elapsed_s"], "degraded": report["degraded"],
                                "model": report.get("model") or ""}, reason=report.get("error") or "")  # fmt: skip
            self.note(f"Ревью {label}: {len(report['issues'])} замечаний за {time.time() - started:.1f} с"
                      + (f" ({report['error']})" if report.get("error") else ""))  # fmt: skip
            if self.cancel.is_set():
                raise runner.Canceled
        self.stage("review_roles", 100)
        self.go("consolidating")

    def previous_issues(self) -> list[dict]:
        if self.rnd["n"] <= 1:
            return []
        prev = state.read_json(self.folder, self.run["run_id"], self.rnd["n"] - 1, "issues.json") or {}
        return prev.get("issues") or []

    def previous_fingerprints(self) -> set[str]:
        out: set[str] = set()
        for n in range(1, self.rnd["n"]):
            plan_data = state.read_json(self.folder, self.run["run_id"], n, "repair_plan.json") or {}
            out.update(a["fingerprint"] for a in plan_data.get("actions") or [] if a["status"] == "done")
        return out

    def consolidate(self) -> None:
        base = self.base_n()
        vdir = versions.folder_of(self.folder, base)
        timeline = _read(vdir / "timeline.json")
        scenes = (_read(vdir / "storyboard.json")).get("scenes") or []
        fps = int(timeline.get("fps") or render.FPS)
        total = int(timeline.get("duration_frames") or 0)
        n_words = len((_read(self.folder / "words.json")).get("words") or [])
        self.stage("review_plan", 0, "замечания")
        self.rnd_stage("consolidate", "running")
        issues: list[dict] = []
        for role_id in self.run["policy"]["reviewer_roles"]:
            report = _read(self.rnd_path("reports") / f"{role_id}.json")
            for issue in report.get("issues") or []:
                problems = issues_module.validate(issue, duration_frames=total, scene_ids={s["id"] for s in scenes},
                                                  n_words=n_words)  # fmt: skip
                if problems:
                    issue["status"], issue["status_reason"] = "invalid", "; ".join(problems)[:200]
                issues.append(issue)
        issues = issues_module.dedup(issues)
        issues_module.apply_policy(issues, self.run["policy"], scenes={s["id"]: s for s in scenes})
        for entry in issues_module.arbitrate(issues):
            self.decide("arbiter", "review:conflict", ids=entry["pair"], rationale=entry["rationale"],
                        output=entry["winner"])  # fmt: skip
        plan_data = planner.plan(
            issues,
            policy=self.run["policy"],
            scenes=scenes,
            fps=fps,
            total_frames=total,
            duration_s=total / fps,
            round_id=self.rnd["round_id"],
            revision_id=self.rnd["base_version"],
            budget_used=self.run["budget"],
            previous_fingerprints=self.previous_fingerprints(),
        )
        self.write_round("issues.json", {"issues": issues, "made_at": round(time.time(), 3)})
        self.write_round("repair_plan.json", plan_data)
        self.rnd["counts"] = issues_module.counts(issues)
        self.decide("validator", "review:consolidate", output=self.rnd["counts"],
                    rationale=f"замечаний {len(issues)}, действий {len(plan_data['actions'])}, "
                              f"утверждение: {plan_data['approval']}")  # fmt: skip
        self.stage("review_plan", 100, f"действий {len(plan_data['actions'])}")
        self.event("issues_consolidated", status="completed", message=f"замечаний {len(issues)}, действий {len(plan_data['actions'])}",
                   metrics={"issues": len(issues), "actions": len(plan_data["actions"]), **self.rnd["counts"]})  # fmt: skip
        self.rnd_stage("consolidate", "done")
        blockers = [i for i in issues if i["severity"] == "blocker" and i["status"] in ("proposed", "accepted")
                    and i["change"]["op"] == "none"]  # fmt: skip
        if blockers:
            self.finish_round("blocked", "незакрытый blocker: " + blockers[0]["observation"][:120])
            self.run["blocking_reason"] = f"blocker без исполнимой правки: {blockers[0]['observation'][:160]}"
            self.event(
                "run_blocked", status="blocked", message="незакрытый blocker", reason=self.run["blocking_reason"] or ""
            )
            self.go("blocked", reason="blocker")
            return
        if not plan_data["actions"]:
            self.finish_round("no_actions", "исполнимых правок нет")
            self.run["stop_reason"] = "no_executable_actions"
            self.go("completed", reason="no_actions")
            return
        if plan_data["approval"] == "required":
            self.rnd_stage("approve", "awaiting", "ждёт человека")
            self.run["blocking_reason"] = "план исправлений ждёт утверждения"
            self.event("approval_required", status="awaiting_approval", message="план исправлений ждёт утверждения",
                       reason="режим " + self.run["policy"]["mode"])  # fmt: skip
            self.go("awaiting_approval", reason="policy")
            return
        if not any(a["status"] == "approved" for a in plan_data["actions"]):
            self.finish_round("deferred", "все действия отложены: " + (plan_data["estimate"]["reason"] or "бюджет"))
            self.run["stop_reason"] = (
                "budget" if not plan_data["estimate"]["within_budget"] else "no_executable_actions"
            )
            self.go("completed", reason=self.run["stop_reason"])
            return
        self.rnd_stage("approve", "done", "по политике")
        self.go("repairing", reason="auto")

    def repairing(self) -> None:
        plan_data = self.read_round("repair_plan.json")
        base = self.base_n()
        timeline = _read(versions.folder_of(self.folder, base) / "timeline.json")
        fps = int(timeline.get("fps") or render.FPS)
        total = int(timeline.get("duration_frames") or 0)
        self.stage("review_repair", 0, "правки манифестов")
        self.rnd_stage("repair", "running")
        self.event("repair_started", message=f"действий к исполнению: {len(planner.ready(plan_data))}")
        if plan_data.get("applied_at"):  # resume: правки уже в проекте
            self.go("rendering", reason="resume")
            return
        board = (_read(self.folder / "storyboard.json")).get("scenes") or []
        self.rnd["hashes_before"] = {s["id"]: s["hash"] for s in board}
        self.rnd["caption_words_before"] = self.project["options"].get("caption_words", 3)
        touched = repair.apply(self.projects, self.project, plan_data, fps=fps, total_frames=total)
        plan_data["applied_at"] = round(time.time(), 3)
        self.write_round("repair_plan.json", plan_data)
        done = [a for a in touched if a["status"] == "done"]
        self.rnd["scenes_touched"] = sorted({a["scene_id"] for a in done if a["scene_id"]})
        self.rnd["frames_changed"] = [r for a in done for r in a["affected_frames"]]
        self.decide("repair_planner", "review:apply", output=[a["action_id"] for a in done],
                    rationale=f"применено {len(done)} из {len(touched)}")  # fmt: skip
        self.engine.relock(self.run)
        self.stage("review_repair", 100, f"применено {len(done)}")
        if not done:
            self.rnd_stage("repair", "failed", "ни одно действие не применилось")
            self.finish_round(
                "rejected", "правки не применились: " + "; ".join(a["error"] or "" for a in touched)[:200]
            )
            self.go("rejected", reason="repair_failed")
            return
        self.rnd_stage("repair", "done")
        self.go("rendering")

    def rendering(self) -> None:
        self.rnd_stage("render", "running")
        self.save()
        started = time.time()
        attempts = int(self.run["policy"]["max_render_attempts_per_issue"])
        ok, error = repair.render(self.projects, self.project, self.cancel, attempts_left=attempts)
        self.run["budget"]["gpu_s"] += round(time.time() - started, 1)
        if not ok:
            self.rnd_stage("render", "failed", error[:200])
            self.rollback(f"пересборка не удалась: {error[:160]}")
            self.go("validating", reason="render_failed")
            self.go("rejected", reason="render_failed")
            return
        board = (_read(self.folder / "storyboard.json")).get("scenes") or []
        new_assets = repair.scenes_rerendered(self.rnd.get("hashes_before") or {}, board)
        self.run["budget"]["new_assets"] += len(new_assets)
        self.rnd["job_ids"].append(f"{self.run['run_id']}-r{self.rnd['n']}-render")
        self.rnd_stage("render", "done", f"перерисовано сцен: {len(new_assets)}")
        self.go("validating")

    def validating(self) -> None:
        self.rnd_stage("validate", "running")
        self.save()
        if not self.rnd.get("candidate_version"):
            known = versions.listing(self.folder)
            n = (known[-1]["n"] + 1) if known else 1
            record = versions.save(self.folder, n, round_n=self.rnd["n"], parent=self.rnd["base_version"],
                                   accepted=None, ffmpeg=self.config.ffmpeg)  # fmt: skip
            self.rnd["candidate_version"] = record["label"]
            self.run["versions"] = list(dict.fromkeys([*self.run["versions"], record["label"]]))
            self.save()
        cand = versions.number(self.rnd["candidate_version"])
        gates = ((versions.get(self.folder, cand) or {}).get("qa") or {}).get("gates") or {}
        if gates.get("mechanical") == "fail":
            self.event("qa_failed", status="failed", severity="warning", message="QA кандидата: механические ошибки",
                       task_id=self.rnd["candidate_version"])  # fmt: skip
        self.stage("review_compare", 0, "бандл кандидата")
        self.ensure_bundle(cand)
        # Повторные замеры кандидата теми же ролями-измерителями: что закрылось, что вернулось.
        ctx = self.context(cand)
        after: set[str] = set()
        for role_id in self.run["policy"]["reviewer_roles"]:
            if reviewers.ROLES[role_id].kind == "llm":
                continue
            report = reviewers.run_role(role_id, ctx, None)
            after.update(i["fingerprint"] for i in report["issues"])
        issues = self.read_round("issues.json").get("issues") or []
        plan_data = self.read_round("repair_plan.json")
        done_ids = {iid for a in plan_data.get("actions") or [] if a["status"] == "done" for iid in a["issue_ids"]}
        before = {i["fingerprint"] for i in issues if i["status"] != "invalid"}
        fixed = [i["issue_id"] for i in issues if i["issue_id"] in done_ids and i["fingerprint"] not in after]
        regressed = sorted(after - before)
        self.rnd["validation"] = {"fixed": fixed, "regressed": regressed, "unresolved": [
            i["issue_id"] for i in issues if i["issue_id"] in done_ids and i["fingerprint"] in after]}  # fmt: skip
        self.rnd_stage("validate", "done", f"закрыто {len(fixed)}, новых {len(regressed)}")
        self.go("comparing")

    def comparing(self) -> None:
        base, cand = self.base_n(), versions.number(self.rnd["candidate_version"])
        self.rnd_stage("compare", "running")
        self.save()
        self.stage("review_compare", 50, "hard gates и метрики")
        sizes = {role: meta["size"] for role, meta in self.project["sources"].items() if meta}
        hard = compare.hard_gates(self.folder, base, cand, policy=self.run["policy"], ffmpeg=self.config.ffmpeg,
                                  source_sizes=sizes)  # fmt: skip
        obj = compare.objective(self.folder, base, cand)
        validation = self.rnd.get("validation") or {"fixed": [], "regressed": []}
        issues = self.read_round("issues.json").get("issues") or []
        fixed_issues = [i for i in issues if i["issue_id"] in validation["fixed"]]
        subj = {"mode": "skipped", "items": [], "profile": self.run["policy"]["review_profile"], "weighted_delta": None,
                "model": "", "prompt_version": None, "summary": "hard gates не пройдены — судья не нужен"}  # fmt: skip
        if all(g["ok"] for g in hard) and not obj["worse"]:
            self.stage("review_compare", 70, "судья")
            diff = versions.diff(self.folder, base, cand)
            ta = bundle.texts(versions.folder_of(self.folder, base), self.folder)
            tb = bundle.texts(versions.folder_of(self.folder, cand), self.folder)
            prompt = compare.judge_prompt(ta, tb, fixed_issues, diff, policy_module.weights(self.run["policy"]))
            subj = compare.subjective(self.run["policy"], self.ask_for("regression_judge"), prompt)
        identical = (versions.get(self.folder, base) or {}).get("output", {}).get("sha256") == (
            versions.get(self.folder, cand) or {}
        ).get("output", {}).get("sha256")
        self.rnd["identical"] = identical
        decision, by, reason = compare.decide(hard, obj, subj, fixed=validation["fixed"],
                                              regressed=validation["regressed"], policy=self.run["policy"],
                                              identical=identical)  # fmt: skip
        if decision == "ambiguous" and self.run["policy"]["mode"] != "manual_approval":
            decision, by, reason = (
                "rejected",
                "policy",
                f"спорно ({reason}) — в режиме {self.run['policy']['mode']} откат",
            )
        record = compare.record(self.rnd["round_id"], versions.label(base), versions.label(cand), hard, obj, subj,
                                fixed=validation["fixed"], regressed=validation["regressed"], decision=decision,
                                decided_by=by, reason=reason)  # fmt: skip
        self.write_round("comparison.json", record)
        self.decide("regression_judge", "review:compare", output=record["subjective"], rationale=f"{decision}: {reason}",
                    confidence=None)  # fmt: skip
        self.rnd["qa_delta"] = {k: obj["candidate"][k] - obj["baseline"][k] for k in obj["baseline"]}
        self.stage("review_compare", 100, decision)
        self.event("comparison_ready", status="completed", message=f"{decision}: {reason}",
                   metrics={"decision": decision, "weighted_delta": subj.get("weighted_delta"), "hard_ok": record["hard_ok"]})  # fmt: skip
        if decision == "ambiguous":
            self.rnd_stage("compare", "awaiting", reason)
            self.run["blocking_reason"] = "сравнение спорное — реши сам: принять кандидата или откатить"
            self.event(
                "approval_required",
                status="awaiting_approval",
                message="сравнение спорное — нужно решение",
                reason=reason,
            )
            self.go("awaiting_decision", reason="ambiguous")
            return
        self.settle(decision, reason, by)

    def settle(self, decision: str, reason: str, by: str) -> None:
        """Принять кандидата или откатить к baseline; статусы замечаний и действий; переход."""
        cand = versions.number(self.rnd["candidate_version"])
        issues = self.read_round("issues.json").get("issues") or []
        plan_data = self.read_round("repair_plan.json")
        validation = self.rnd.get("validation") or {"fixed": [], "regressed": []}
        done_ids = {iid for a in plan_data.get("actions") or [] if a["status"] == "done" for iid in a["issue_ids"]}
        if decision == "accepted":
            versions.mark(self.folder, cand, accepted=True, reason=reason)
            self.run["accepted_version"] = versions.label(cand)
            for issue in issues:
                if issue["issue_id"] in validation["fixed"]:
                    issue["status"], issue["status_reason"] = "fixed", reason[:120]
                elif issue["issue_id"] in done_ids:
                    issue["status"], issue["status_reason"] = "failed", "правка применена, замечание осталось"
        else:
            versions.mark(self.folder, cand, accepted=False, reason=reason)
            self.rollback(reason)
            for issue in issues:
                if issue["issue_id"] in done_ids:
                    issue["status"] = "regressed" if issue["issue_id"] in validation.get("regressed", []) else "failed"
                    issue["status_reason"] = f"кандидат отклонён: {reason[:100]}"
        self.write_round("issues.json", {"issues": issues, "made_at": round(time.time(), 3)})
        self.rnd["counts"] = issues_module.counts(issues)
        self.decide("human" if by == "human" else "regression_judge", f"review:{decision}",
                    ids=[self.rnd["candidate_version"]], rationale=reason)  # fmt: skip
        self.rnd_stage("compare", "done", decision)
        self.event(f"revision_{decision}", status="completed", message=f"{self.rnd['candidate_version']}: {reason}",
                   metrics={"fixes": len(validation["fixed"]), "by": by}, task_id=self.rnd["candidate_version"])  # fmt: skip
        self.finish_round(decision, reason)
        self.go(decision, reason=by)

    def rollback(self, reason: str) -> None:
        """Откат манифестов и ролика к baseline раунда: следующий раунд — от последней принятой версии."""
        base = self.base_n()
        versions.restore(self.folder, base)
        with self.projects._lock:
            words = self.rnd.get("caption_words_before", self.project["options"].get("caption_words", 3))
            self.project["options"]["caption_words"] = words
            board = (_read(self.folder / "storyboard.json")).get("scenes") or []
            self.project["storyboard"] = storyboard.summary(board) if board else None
            self.projects._touch(self.project)
        self.engine.relock(self.run)
        self.note(f"Откат к {versions.label(base)}: {reason}")

    def finish_round(self, decision: str, reason: str) -> None:
        self.rnd["decision"] = decision
        self.rnd["decision_reason"] = reason[:300]
        self.rnd["finished_at"] = round(time.time(), 3)
        self.rnd["state"] = decision if decision in ("accepted", "rejected") else "completed"
        for stage in self.rnd["stages"]:
            if stage["status"] in ("pending", "running", "awaiting"):
                stage["status"] = "skipped"
        self.save()

    def next_round(self) -> None:
        """Ранняя остановка или следующий раунд (§8)."""
        policy = self.run["policy"]
        reason = ""
        comparison = self.read_round("comparison.json")
        subj = comparison.get("subjective") or {}
        fixed = comparison.get("fixed_issues") or []
        if self.run.get("stop_reason") == "user":
            reason = "user"
        elif self.rnd["n"] >= policy["max_rounds"]:
            reason = "max_rounds"
        elif policy_module.exhausted(policy, self.run["budget"]):
            reason = "budget"
        elif policy["stop_when_no_material_gain"] and (
            self.rnd.get("identical")
            or (self.rnd["decision"] == "accepted" and not fixed and (subj.get("weighted_delta") or 0) <= compare.GAIN)
        ):
            reason = "no_material_gain"
        elif self.repeated_issues():
            reason = "repeated_issues"
        elif self.oscillating():
            reason = "oscillation"
        self.run["budget"]["wall_s"] = round(time.time() - (self.run["started_at"] or time.time()), 1)
        if reason:
            self.run["stop_reason"] = reason
            self.run["blocking_reason"] = None
            self.decide("validator", "review:stop", rationale=reason)
            self.event("run_completed", status="completed", message=f"ревью завершено: {reason}", reason=reason,
                       metrics={"rounds": len(self.run["rounds"]), "planned": policy["max_rounds"]})  # fmt: skip
            self.go("completed", reason=reason)
            return
        self.go("preparing", reason="next_round")

    def repeated_issues(self) -> bool:
        if self.rnd["n"] < 2:
            return False
        now = {i["fingerprint"] for i in self.read_round("issues.json").get("issues") or [] if i["status"] != "invalid"}
        prev = state.read_json(self.folder, self.run["run_id"], self.rnd["n"] - 1, "issues.json") or {}
        before = {i["fingerprint"] for i in prev.get("issues") or [] if i["status"] != "invalid"}
        return bool(now) and len(now & before) / len(now) >= REPEAT_SHARE

    def oscillating(self) -> bool:
        if self.rnd["n"] < 2:
            return False
        last = [state.read_json(self.folder, self.run["run_id"], n, "repair_plan.json") or {}
                for n in (self.rnd["n"] - 1, self.rnd["n"])]  # fmt: skip
        notes = [n for p in last for n in (p.get("policy") or {}).get("notes") or []]
        return sum(1 for n in notes if "осцилляция" in n) >= 2


def drive(review, project: dict, run_id: str, cancel: threading.Event) -> str:
    folder = review.folder(project["id"])
    run = state.load_run(folder, run_id)
    if run is None:
        raise runner.RunFailed("Запуск ревью пропал из каталога проекта.")
    if run["state"] == "paused":
        return LABELS["paused"]
    if run["state"] in (*state.WAITING, *state.FINAL, "blocked"):
        return LABELS.get(run["state"], "Готово")
    if not review.check_lock(run):
        return LABELS["blocked"]
    ctx = Round(review, project, run, cancel)
    steps = {
        "preparing": ctx.prepare, "reviewing": ctx.review, "consolidating": ctx.consolidate,
        "repairing": ctx.repairing, "rendering": ctx.rendering, "validating": ctx.validating,
        "comparing": ctx.comparing, "accepted": ctx.next_round, "rejected": ctx.next_round,
    }  # fmt: skip
    try:
        while run["state"] in steps:
            if cancel.is_set():
                raise runner.Canceled
            steps[run["state"]]()
    except runner.Canceled:
        on_cancel(review, folder, run)
        raise
    except (runner.RunFailed, llm.StudioError, ValueError) as exc:
        fail(folder, run, str(exc))
        raise
    except Exception as exc:  # noqa: BLE001 — внутренняя ошибка: запуск failed, воркер пишет трассу
        fail(folder, run, f"внутренняя ошибка: {type(exc).__name__}")
        raise
    return LABELS.get(run["state"], "Готово")


def fail(folder: Path, run: dict, error: str) -> None:
    run["error"] = error[:500]
    if state.can(run, "failed"):
        state.advance(run, "failed", reason="error")
    state.checkpoint(folder, run)


def on_cancel(review, folder: Path, run: dict) -> None:
    """Пауза или стоп человека: пауза уже записана engine.control, стоп — завершаем запуск."""
    fresh = state.load_run(folder, run["run_id"]) or run
    if fresh.get("stop_reason") == "user" and fresh["state"] not in state.FINAL:
        target = "completed" if any(r.get("decision") for r in fresh["rounds"]) else "cancelled"
        if state.can(fresh, target):
            state.advance(fresh, target, reason="user", blocking_reason=None)
        state.checkpoint(folder, fresh)
    elif fresh["state"] != "paused" and state.can(fresh, "paused"):
        fresh["blocking_reason"] = "остановлено на чекпоинте"
        state.advance(fresh, "paused", reason="cancel")
        state.checkpoint(folder, fresh)
