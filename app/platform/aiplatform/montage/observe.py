"""Ручки наблюдаемости «Монтажа» (ITERATIVE_REVIEW_SPEC §14.6): прогресс, события, SSE, экспорт, ресурсы, агенты.

`Observer` живёт в `MontageProjects.observe` и только читает: дерево прогресса из событий и текущих
стадий проекта, страницу событий по курсору и фильтрам, SSE-поток (снапшот дерева + backfill после
`last_event_id` + живые события + heartbeat; поток закрывается через STREAM_MAX_S — клиент переподключится
с курсором), экспорт JSONL/CSV, ресурсы машины, карточки агентов, историю запусков, прогресс раунда.
Backend — единственный источник истины: UI ничего не досчитывает.
"""

from __future__ import annotations

import json
import time

from aiplatform.montage import progress
from aiplatform.montage.review import reviewers, state

HEARTBEAT_S = 10.0
STREAM_MAX_S = 600.0
SNAPSHOT_EVENTS = 200
AGENT_LABELS = {"ffmpeg": "ffmpeg (сборка)", "asr": "расшифровка", "llm": "LLM", "hyperframes": "HyperFrames",
                "rough_cut": "план вырезок (LLM)", "research": "карта знаний (LLM)", "narrative": "биты (LLM)",
                "art_director": "стиль (LLM)", "storyboard": "раскадровка (LLM)", "semantic_qa": "проверка (LLM)",
                "motion": "вставки (LLM)", "regression_judge": "судья версий (LLM)"}  # fmt: skip


def _sse(event: str, data: object, event_id: int | None = None) -> str:
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class Observer:
    def __init__(self, projects) -> None:
        self.projects = projects

    def _project(self, pid: str) -> dict | None:
        return self.projects.get(pid)

    def tree(self, pid: str) -> dict | None:
        project = self._project(pid)
        if project is None:
            return None
        events = list(self.projects.events_of(pid).iter_all())
        stats = self.projects._stats
        root = progress.tree(project, events, stats=stats)
        stage_list = project.get("stages") or []
        return {
            "tree": root,
            "generated_at": round(time.time(), 3),
            "last_seq": self.projects.events_of(pid).last_seq,
            "stages": stage_list,
            "eta": progress.eta(stage_list, stats) if project["status"] == "running" else None,
            "status": project["status"],
        }

    def events(self, pid: str, query: dict) -> dict | None:
        if self._project(pid) is None:
            return None
        try:
            cursor = int((query.get("cursor") or ["0"])[0])
            limit = int((query.get("limit") or ["500"])[0])
        except ValueError:
            cursor, limit = 0, 500
        return self.projects.events_of(pid).read(cursor=cursor, limit=limit, filters=progress.parse_filters(query))

    def stream(self, pid: str, last_event_id: int, filters: dict | None = None):
        """Генератор SSE: retry, snapshot, backfill, живые события, `: ping` каждые HEARTBEAT_S."""
        events = self.projects.events_of(pid)
        cursor = max(0, last_event_id)
        yield "retry: 3000\n\n"
        snapshot = self.tree(pid)
        yield _sse("snapshot", snapshot, cursor)
        started = time.monotonic()
        while not self.projects.closing.is_set() and time.monotonic() - started < STREAM_MAX_S:
            page = events.read(cursor=cursor, limit=SNAPSHOT_EVENTS, filters=filters)
            for item in page["events"]:
                cursor = item["seq"]
                yield _sse(item["type"], item, item["seq"])
            if page["has_more"]:
                continue
            if cursor < events.last_seq:  # отфильтрованные события: двигаем курсор, чтобы не перечитывать их
                cursor = events.last_seq
            if not events.wait(cursor, HEARTBEAT_S):
                yield ": ping\n\n"
        yield _sse("bye", {"reason": "stream_max" if not self.projects.closing.is_set() else "closing"}, cursor)

    def export(self, pid: str, fmt: str, query: dict):
        if self._project(pid) is None:
            return None
        return self.projects.events_of(pid).export("csv" if fmt == "csv" else "jsonl", progress.parse_filters(query))

    def resources(self, pid: str | None = None) -> dict:
        gate = self.projects._gate
        held = gate.held()
        return progress.resources(
            self.projects.config.data_dir,
            queue_depth=self.projects._queue.qsize(),
            gpu_waiting=gate.waiting(),
            gpu_holder=held["owner"] if held else None,
            last_job=self.projects.last_job(pid) if pid else None,
            retries=self.projects.retries,
        )

    def agents(self, pid: str) -> dict | None:
        if self._project(pid) is None:
            return None
        cards: dict[str, dict] = {}
        for ev in self.projects.events_of(pid).iter_all():
            agent = ev.get("agent_id")
            if not agent:
                continue
            card = cards.get(agent) or {"id": agent, "label": _label(agent), "kind": _kind(agent), "status": "pending",
                                        "current": "", "done": 0, "failed": 0, "issues": 0, "fixes": 0, "attempts": 0,
                                        "latency_s": None, "model": "", "last_at": None, "last_event": ""}  # fmt: skip
            if ev["status"] != "none":
                card["status"] = ev["status"]
            card["current"] = ev["message"] if ev["status"] == "running" else ""
            card["last_event"], card["last_at"] = ev["type"], ev["occurred_at"]
            metrics = ev.get("metrics") or {}
            if ev["type"] in ("job_finished", "reviewer_finished"):
                card["done"] += 1
                if metrics.get("elapsed_s") is not None:
                    card["latency_s"] = metrics["elapsed_s"]
            if ev["type"] == "job_failed":
                card["failed"] += 1
            card["issues"] += int(metrics.get("issues") or 0)
            card["fixes"] += int(metrics.get("fixes") or 0)
            card["attempts"] = max(card["attempts"], int(ev.get("attempt") or 0))
            if metrics.get("model"):
                card["model"] = metrics["model"]
            cards[agent] = card
        return {"agents": list(cards.values()), "roles": reviewers.public()}

    def runs(self, pid: str) -> dict | None:
        """История запусков действий по событиям run_*."""
        if self._project(pid) is None:
            return None
        runs: dict[str, dict] = {}
        for ev in self.projects.events_of(pid).iter_all():
            rid = ev.get("run_id")
            if not rid:
                continue
            run = runs.get(rid) or {"run_id": rid, "action": ev.get("phase_id"), "status": "queued",
                                    "started_at": ev["occurred_at"], "finished_at": None, "reason": "", "stages": [],
                                    "rounds": [], "errors": 0, "jobs": 0}  # fmt: skip
            if ev["type"].startswith("run_") or ev["type"] == "approval_required":
                if ev["status"] != "none":
                    run["status"] = ev["status"]
                if ev["reason"]:
                    run["reason"] = ev["reason"]
                if ev["status"] in ("completed", "failed", "cancelled", "blocked", "awaiting_approval"):
                    run["finished_at"] = ev["occurred_at"]
            if ev.get("stage_id") and ev["stage_id"] not in run["stages"]:
                run["stages"].append(ev["stage_id"])
            if ev.get("round_id") and ev["round_id"] not in run["rounds"]:
                run["rounds"].append(ev["round_id"])
            if ev["severity"] == "error":
                run["errors"] += 1
            if ev["type"] == "job_started":
                run["jobs"] += 1
            runs[rid] = run
        out = list(runs.values())
        for run in out:
            end = run["finished_at"] or (time.time() if run["status"] in ("running", "queued") else None)
            run["duration_s"] = round(end - run["started_at"], 1) if end else None
        return {"runs": out[::-1]}

    def round_progress(self, pid: str, run_id: str, n: str) -> dict | None:
        """Прогресс раунда ревью: стадии с временем, счётчики, события раунда."""
        if self._project(pid) is None:
            return None
        rnd = state.load_round(self.projects.dir(pid), run_id, int(n) if str(n).isdigit() else 0)
        if rnd is None:
            return None
        round_events = [ev for ev in self.projects.events_of(pid).iter_all() if ev.get("round_id") == rnd["round_id"]]
        agents = {}
        for ev in round_events:
            if ev.get("agent_id"):
                agents[ev["agent_id"]] = {"status": ev["status"], "message": ev["message"], "at": ev["occurred_at"]}
        return {
            "round": rnd,
            "stages": [
                {
                    **s,
                    "elapsed_s": round((s["finished_at"] or time.time()) - s["started_at"], 1)
                    if s.get("started_at")
                    else None,
                }
                for s in rnd["stages"]
            ],  # fmt: skip
            "agents": agents,
            "events": round_events[-SNAPSHOT_EVENTS:],
            "planned": None,
        }


def _label(agent: str) -> str:
    role = reviewers.ROLES.get(agent.removeprefix("review_"))
    if role and agent.startswith("review_"):
        return f"{role.label} (ревью)"
    return AGENT_LABELS.get(agent, agent)


def _kind(agent: str) -> str:
    if agent.startswith("review_"):
        role = reviewers.ROLES.get(agent.removeprefix("review_"))
        return role.kind if role else "llm"
    return {"ffmpeg": "process", "asr": "process", "hyperframes": "process"}.get(agent, "llm")
