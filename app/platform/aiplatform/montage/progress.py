"""Наблюдаемость конвейера «Монтажа» (ITERATIVE_REVIEW_SPEC §14): события, дерево прогресса, ETA, ресурсы.

`Events` — append-only `events.jsonl` проекта: `seq` растёт монотонно (курсор), `event_id` =
`<проект>-<seq>`, ротация по числу строк (хвост в `events.jsonl.1`), фильтры, экспорт JSONL/CSV, ожидание
новых событий для SSE. `Tracker` — что пишет воркер: запуск действия, стадии (progress не чаще раза в
секунду на стадию, переходы — всегда), подпроцессы (jobs: fps и speed ffmpeg из `-progress`), задачи
(сцена, сегмент, роль), попытки, логи. `tree` — Project → Run → Phase → Round → Stage → Job/Task →
Attempt из событий и `project.stages`: статусы, счётчики, проценты по единицам, ETA с уверенностью.
`Stats` — секунды на единицу веса стадии на этой машине (EMA в `montage/stats.json`): мало замеров —
`confidence: low`, fallback на веса `stages.py`. `resources` — только измеримое: loadavg, память, диск,
очередь, fps последнего рендера; GPU — `unavailable` (без powermetrics API нет).
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import schemas, stages

FILE = "events.jsonl"
ROTATE_LINES = 200_000
KEEP_BACKUPS = 1
PROGRESS_EVERY_S = 1.0
STATUSES = ("pending", "queued", "running", "awaiting_approval", "paused", "retrying", "blocked", "failed",
            "cancelled", "skipped", "completed", "none")  # fmt: skip
PHASES = {
    "prepare": "ingest", "replan": "rough_cut", "revise": "rough_cut", "story": "storyboard", "scene": "storyboard",
    "render": "render", "export": "export", "review": "review",
}  # fmt: skip
STAGE_PHASE = {
    "audio": "ingest", "sync": "ingest", "silences": "transcription", "transcribe": "transcription", "plan": "rough_cut",
    "timemap": "semantic", "knowledge": "semantic", "beats": "semantic", "style": "storyboard", "storyboard": "storyboard",
    "segments": "render", "join": "render", "final": "render", "check": "qa", "motion": "motion", "scenes": "motion",
    "qa": "qa", "export": "export", "review_bundle": "review", "review_roles": "review", "review_plan": "review",
    "review_repair": "repair", "review_compare": "comparison",
}  # fmt: skip
ROUND_PHASE = {  # стадии действия review → стадии раунда (analyze → … → compare), как в review_round
    "review_bundle": "analyze", "review_roles": "analyze", "review_plan": "consolidate", "review_repair": "repair",
    "segments": "render", "join": "render", "final": "render", "scenes": "render", "motion": "render", "check": "render",
    "qa": "validate", "review_compare": "compare",
}  # fmt: skip
ROUND_STATUS_EVENTS = ("approval_required", "run_blocked", "run_completed", "run_failed", "run_cancelled")
STAGE_UNITS = {"transcribe": "seconds", "segments": "segments", "scenes": "scenes", "review_roles": "roles",
               "check": "seconds", "export": "seconds"}  # fmt: skip
FINAL_STATUSES = ("completed", "failed", "cancelled", "skipped", "blocked")
_FF_FPS = re.compile(r"^fps=\s*(\d+(?:\.\d+)?)")
_FF_SPEED = re.compile(r"^speed=\s*(\d+(?:\.\d+)?)x")
CSV_FIELDS = ("seq", "occurred_at", "type", "severity", "status", "run_id", "phase_id", "round_id", "stage_id",
              "agent_id", "job_id", "task_id", "attempt", "correlation_id", "message", "progress", "metrics", "reason")  # fmt: skip


def _read_lines(path: Path):
    try:
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict) and "seq" in item:
                    yield item
    except OSError:
        return


class Events:
    """Журнал одного проекта: append с seq, чтение по курсору и фильтрам, ожидание новых, ротация, экспорт."""

    def __init__(self, folder: Path, project_id: str, *, rotate_lines: int = ROTATE_LINES) -> None:
        self.folder = folder
        self.project_id = project_id
        self.path = folder / FILE
        self.rotate_lines = rotate_lines
        self._cond = threading.Condition()
        self._seq = 0
        self._lines = 0
        self._last_progress: dict[str, float] = {}
        self._fh = None  # открытый на дозапись файл: 100 000 событий без open/close на каждое
        self._scan()

    def _scan(self) -> None:
        last = 0
        lines = 0
        for item in _read_lines(self.path):
            last = max(last, int(item["seq"]))
            lines += 1
        for backup in self._backups():
            for item in _read_lines(backup):
                last = max(last, int(item["seq"]))
        self._seq, self._lines = last, lines

    def _backups(self) -> list[Path]:
        return [p for n in range(KEEP_BACKUPS, 0, -1) if (p := self.path.with_name(f"{FILE}.{n}")).is_file()]

    @property
    def last_seq(self) -> int:
        return self._seq

    def append(self, type_: str, *, severity: str = "info", status: str = "none", message: str = "",
               progress: dict | None = None, metrics: dict | None = None, artifact_ids=(), reason: str = "",
               attempt: int = 0, **ids) -> dict | None:  # fmt: skip
        with self._cond:
            seq = self._seq + 1
            record = {
                "event_id": f"{self.project_id}-{seq}",
                "seq": seq,
                "occurred_at": round(time.time(), 3),
                "project_id": self.project_id,
                "run_id": ids.get("run_id"),
                "phase_id": ids.get("phase_id"),
                "round_id": ids.get("round_id"),
                "stage_id": ids.get("stage_id"),
                "agent_id": ids.get("agent_id"),
                "job_id": ids.get("job_id"),
                "task_id": ids.get("task_id"),
                "attempt": attempt,
                "correlation_id": ids.get("correlation_id") or ids.get("run_id"),
                "type": type_,
                "severity": severity if severity in ("debug", "info", "warning", "error") else "info",
                "status": status if status in STATUSES else "none",
                "message": str(message)[:500],
                "progress": progress,
                "metrics": metrics or {},
                "artifact_ids": list(artifact_ids),
                "reason": str(reason)[:300],
            }
            if not self._write(record):
                return None
            self._seq = seq
            self._lines += 1
            if self._lines >= self.rotate_lines:
                self._rotate()
            self._cond.notify_all()
        return record

    def throttled(self, key: str, *, every: float = PROGRESS_EVERY_S) -> bool:
        """True — событие progress по этому ключу можно писать (прошло ≥ every секунд)."""
        now = time.monotonic()
        last = self._last_progress.get(key, 0.0)
        if now - last < every:
            return False
        self._last_progress[key] = now
        return True

    def _write(self, record: dict) -> bool:
        try:
            if self._fh is None:
                self.folder.mkdir(parents=True, exist_ok=True)
                self._fh = self.path.open("a", encoding="utf-8")
            self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._fh.flush()
            return True
        except OSError:
            self.close()
            return False

    def close(self) -> None:
        if self._fh is not None:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None

    def _rotate(self) -> None:
        self.close()
        for n in range(KEEP_BACKUPS, 0, -1):
            older = self.path.with_name(f"{FILE}.{n}")
            if n == KEEP_BACKUPS:
                older.unlink(missing_ok=True)
            else:
                newer = self.path.with_name(f"{FILE}.{n + 1}")
                if older.is_file():
                    older.replace(newer)
        if self.path.is_file():
            self.path.replace(self.path.with_name(f"{FILE}.1"))
        self._lines = 0

    def iter_all(self):
        for backup in self._backups():
            yield from _read_lines(backup)
        yield from _read_lines(self.path)

    def read(self, *, cursor: int = 0, limit: int = 500, filters: dict | None = None) -> dict:
        """События с seq > cursor по фильтрам; limit ≤ 5000; has_more — есть ли дальше."""
        limit = max(1, min(int(limit or 500), 5000))
        out: list[dict] = []
        has_more = False
        for item in self.iter_all():
            if item["seq"] <= cursor or not matches(item, filters or {}):
                continue
            if len(out) >= limit:
                has_more = True
                break
            out.append(item)
        return {"events": out, "next_cursor": out[-1]["seq"] if out else cursor, "has_more": has_more,
                "last_seq": self._seq}  # fmt: skip

    def tail(self, n: int = 50) -> list[dict]:
        items = list(self.iter_all())
        return items[-n:]

    def wait(self, cursor: int, timeout: float) -> bool:
        """Ждать появления событий после cursor; False — таймаут."""
        with self._cond:
            if self._seq > cursor:
                return True
            self._cond.wait(timeout)
            return self._seq > cursor

    def export(self, fmt: str, filters: dict | None = None):
        """Итератор строк экспорта: jsonl — как в файле; csv — плоские поля."""
        if fmt == "csv":
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            yield buf.getvalue()
            for item in self.iter_all():
                if not matches(item, filters or {}):
                    continue
                buf.seek(0)
                buf.truncate()
                flat = {**item, "progress": json.dumps(item.get("progress"), ensure_ascii=False),
                        "metrics": json.dumps(item.get("metrics"), ensure_ascii=False)}  # fmt: skip
                writer.writerow(flat)
                yield buf.getvalue()
            return
        for item in self.iter_all():
            if matches(item, filters or {}):
                yield json.dumps(item, ensure_ascii=False) + "\n"


def matches(item: dict, filters: dict) -> bool:
    for key in ("type", "stage_id", "agent_id", "job_id", "task_id", "severity", "status", "round_id", "run_id",
                "correlation_id"):  # fmt: skip
        want = filters.get(key)
        if want and item.get(key) not in (want if isinstance(want, list | tuple | set) else {want}):
            return False
    if filters.get("min_severity"):
        order = ("debug", "info", "warning", "error")
        if order.index(item["severity"]) < order.index(filters["min_severity"]):
            return False
    if filters.get("since") and item["occurred_at"] < float(filters["since"]):
        return False
    if filters.get("until") and item["occurred_at"] > float(filters["until"]):
        return False
    q = (filters.get("q") or "").lower()
    return not (q and q not in json.dumps(item, ensure_ascii=False).lower())


def parse_filters(query: dict) -> dict:
    """Фильтры из query string ручки событий (значения — первый элемент списка)."""
    out = {}
    for key in ("type", "stage_id", "agent_id", "job_id", "task_id", "severity", "status", "round_id", "run_id",
                "correlation_id", "min_severity", "q"):  # fmt: skip
        value = query.get(key)
        if value and value[0]:
            out[key] = value[0]
    for key in ("since", "until"):
        value = query.get(key)
        try:
            if value and value[0]:
                out[key] = float(value[0])
        except ValueError:
            continue
    return out


# ---------- статистика машины и ETA ----------


class Stats:
    """EMA секунд на единицу веса каждой стадии на этой машине (`montage/stats.json`)."""

    LOW, MEDIUM = 3, 10

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        try:
            self.data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        if not isinstance(self.data, dict):
            self.data = {}

    def observe(self, stage_id: str, seconds: float, weight: float) -> None:
        if seconds <= 0 or weight <= 0:
            return
        per = seconds / weight
        with self._lock:
            item = self.data.get(stage_id) or {"per_weight_s": per, "samples": 0}
            n = item["samples"]
            alpha = 0.3 if n else 1.0
            item["per_weight_s"] = round(item["per_weight_s"] * (1 - alpha) + per * alpha, 4)
            item["samples"] = n + 1
            item["last_s"] = round(seconds, 2)
            self.data[stage_id] = item
            with contextlib.suppress(OSError):
                atomic_write(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))

    def per_weight(self, stage_id: str) -> tuple[float | None, int]:
        item = self.data.get(stage_id) or {}
        return item.get("per_weight_s"), int(item.get("samples") or 0)


def eta(stage_list: list[dict], stats: Stats | None, now: float | None = None) -> dict:
    """{seconds, low, high, confidence, estimated_finish, basis}: текущая стадия по скорости, впереди — по
    истории машины (мало замеров — low) или по весам (basis: weights)."""
    now = time.time() if now is None else now
    current = stages.current(stage_list)
    if current is None:
        return {"seconds": None, "low": None, "high": None, "confidence": "none", "estimated_finish": None,
                "basis": "idle"}  # fmt: skip
    samples_min = 10**9
    ahead = 0.0
    basis = "history"
    for s in stage_list:
        if s["status"] != "pending":
            continue
        per, n = stats.per_weight(s["id"]) if stats else (None, 0)
        if per is None:
            basis = "weights"
            per = stages.remaining([*stage_list], now) or 0  # грубо: fallback ниже
            samples_min = 0
            ahead = None
            break
        samples_min = min(samples_min, n)
        ahead += s["weight"] * per
    here = stages.stage_remaining(current, now)
    if ahead is None:
        total = stages.remaining(stage_list, now)
        if total is None:
            return {"seconds": None, "low": None, "high": None, "confidence": "low", "estimated_finish": None,
                    "basis": "weights"}  # fmt: skip
        return {"seconds": total, "low": round(total * 0.6), "high": round(total * 1.6), "confidence": "low",
                "estimated_finish": round(now + total), "basis": "weights"}  # fmt: skip
    if here is None:
        per, n = stats.per_weight(current["id"]) if stats else (None, 0)
        if per is None:
            return {"seconds": None, "low": None, "high": None, "confidence": "low", "estimated_finish": None,
                    "basis": basis}  # fmt: skip
        samples_min = min(samples_min, n)
        here = max(0.0, current["weight"] * per - (now - (current["started_at"] or now)))
    total = round(here + ahead)
    confidence = "low" if samples_min < Stats.LOW else "medium" if samples_min < Stats.MEDIUM else "high"
    spread = {"low": 0.5, "medium": 0.25, "high": 0.12}[confidence]
    return {"seconds": total, "low": round(total * (1 - spread)), "high": round(total * (1 + spread)),
            "confidence": confidence, "estimated_finish": round(now + total), "basis": basis}  # fmt: skip


# ---------- дерево прогресса ----------


def _node(nid: str, kind: str, title: str, *, parent: str | None, status: str = "pending", **extra) -> dict:
    return {
        "id": nid, "kind": kind, "title": title, "parent_id": parent, "status": status,
        "started_at": None, "updated_at": None, "finished_at": None, "elapsed_s": None,
        "counts": {"total": 0, "completed": 0, "running": 0, "failed": 0, "skipped": 0, "remaining": 0},
        "percent": None, "indeterminate": False, "eta": None, "blocking_reason": None, "current_action": "",
        "artifacts": [], "correlation_id": None, "children": [], **extra,
    }  # fmt: skip


def _touch(node: dict, event: dict) -> None:
    t = event["occurred_at"]
    node["started_at"] = node["started_at"] or t
    node["updated_at"] = max(node["updated_at"] or 0, t)
    if event["status"] in FINAL_STATUSES:
        node["finished_at"] = t
    node["correlation_id"] = node["correlation_id"] or event.get("correlation_id")


def _counts(node: dict) -> None:
    kids = node["children"]
    for kid in kids:
        _counts(kid)
    if not kids:
        return
    c = {"total": len(kids), "completed": 0, "running": 0, "failed": 0, "skipped": 0}
    for kid in kids:
        st = kid["status"]
        if st == "completed":
            c["completed"] += 1
        elif st in ("running", "retrying", "queued"):
            c["running"] += 1
        elif st in ("failed", "cancelled", "blocked"):
            c["failed"] += 1
        elif st == "skipped":
            c["skipped"] += 1
    c["remaining"] = c["total"] - c["completed"] - c["skipped"]
    node["counts"] = c
    if node["percent"] is None and node["kind"] in ("run", "phase", "round", "project"):
        done = sum(1 for k in kids if k["status"] in ("completed", "skipped"))
        partial = sum((k["percent"] or 0) / 100 for k in kids if k["status"] in ("running", "retrying"))
        node["percent"] = round(100 * (done + partial) / len(kids), 1) if kids else None


def tree(project: dict, events_list: list[dict], *, stats: Stats | None = None, now: float | None = None) -> dict:
    """Дерево прогресса из событий и текущих стадий проекта (§14.2); 100 % только после completed."""
    now = time.time() if now is None else now
    root = _node(project["id"], "project", project.get("title") or project["id"], parent=None,
                 status=_project_status(project))  # fmt: skip
    runs: dict[str, dict] = {}
    phases: dict[tuple[str, str], dict] = {}
    rounds: dict[tuple[str, str], dict] = {}
    stage_nodes: dict[tuple[str, str], dict] = {}
    jobs: dict[str, dict] = {}
    tasks: dict[tuple[str, str], dict] = {}
    for ev in events_list:
        rid = ev.get("run_id")
        if not rid:
            continue
        run = runs.get(rid)
        if run is None:
            run = runs[rid] = _node(rid, "run", ev.get("phase_id") or "run", parent=root["id"], status="queued",
                                    action=ev.get("phase_id"))  # fmt: skip
            root["children"].append(run)
        _touch(run, ev)
        if ev["type"] in ("run_queued", "run_started", "run_finished", "run_failed", "run_cancelled", "run_blocked",
                          "run_completed", "approval_required"):  # fmt: skip
            run["status"] = ev["status"] if ev["status"] != "none" else run["status"]
            run["current_action"] = ev["message"]
            if ev["reason"]:
                run["blocking_reason"] = ev["reason"]
            if ev["type"] == "run_queued" and ev.get("metrics", {}).get("stages"):
                run["planned_stages"] = ev["metrics"]["stages"]
        parent = run
        if ev.get("round_id"):
            key = (rid, ev["round_id"])
            rnd = rounds.get(key)
            if rnd is None:
                rnd = rounds[key] = _node(f"{rid}:{ev['round_id']}", "round", ev["round_id"], parent=run["id"],
                                          status="running")  # fmt: skip
                run["children"].append(rnd)
            _touch(rnd, ev)
            if ev["type"].startswith("round_") or ev["type"] in ROUND_STATUS_EVENTS:
                rnd["status"] = ev["status"] if ev["status"] != "none" else rnd["status"]
                rnd["current_action"] = ev["message"]
                if ev["reason"]:
                    rnd["blocking_reason"] = ev["reason"]
            if ev["type"] == "round_started" and ev.get("metrics", {}).get("stages"):
                for stage_id in ev["metrics"]["stages"]:  # план раунда: фазы заранее, чтобы процент был честным
                    pkey = (rnd["id"], stage_id)
                    if pkey not in phases:
                        phases[pkey] = _node(
                            f"{rnd['id']}:{stage_id}", "phase", stage_id, parent=rnd["id"], status="pending"
                        )
                        rnd["children"].append(phases[pkey])
            if ev["type"] in ("revision_accepted", "revision_rejected"):
                for phase in rnd["children"]:
                    if phase["status"] == "pending":
                        phase["status"] = "skipped"
            parent = rnd
        if ev.get("stage_id"):
            phase_id = (ROUND_PHASE if ev.get("round_id") else STAGE_PHASE).get(
                ev["stage_id"], ev.get("phase_id") or "other"
            )
            pkey = (parent["id"], phase_id)
            phase = phases.get(pkey)
            if phase is None:
                phase = phases[pkey] = _node(f"{parent['id']}:{phase_id}", "phase", phase_id, parent=parent["id"],
                                             status="running")  # fmt: skip
                parent["children"].append(phase)
            elif phase["status"] == "pending":
                phase["status"] = "running"
            _touch(phase, ev)
            skey = (parent["id"], ev["stage_id"])
            node = stage_nodes.get(skey)
            if node is None:
                label = stages.STAGES.get(ev["stage_id"], (ev["stage_id"], 0))[0]
                node = stage_nodes[skey] = _node(f"{parent['id']}:{ev['stage_id']}", "stage", label, parent=phase["id"],
                                                 status="running", stage_id=ev["stage_id"])  # fmt: skip
                phase["children"].append(node)
            _touch(node, ev)
            if ev["type"] in ("stage_started", "stage_progress", "stage_finished"):
                node["status"] = ev["status"] if ev["status"] != "none" else node["status"]
                node["current_action"] = ev["message"]
                prog = ev.get("progress") or {}
                if prog.get("total_units"):
                    node["percent"] = round(100 * prog["completed_units"] / prog["total_units"], 1)
                    if prog.get("unit") != "percent" or "units" not in node:  # единицы стадии важнее процентов
                        node["units"] = prog
                elif prog:
                    node["indeterminate"] = True
                if ev["status"] == "completed":
                    node["percent"] = 100.0
                if ev["reason"]:
                    node["blocking_reason"] = ev["reason"]
            parent = node
        if ev.get("job_id"):
            job = jobs.get(ev["job_id"])
            if job is None:
                job = jobs[ev["job_id"]] = _node(ev["job_id"], "job", ev.get("agent_id") or ev["job_id"], parent=parent["id"],
                                                  status="running", agent_id=ev.get("agent_id"), metrics={})  # fmt: skip
                parent["children"].append(job)
            _touch(job, ev)
            if ev["type"].startswith("job_"):
                job["status"] = ev["status"] if ev["status"] != "none" else job["status"]
                job["current_action"] = ev["message"]
                job["metrics"] = {**job.get("metrics", {}), **(ev.get("metrics") or {})}
                prog = ev.get("progress") or {}
                if prog.get("total_units"):
                    job["percent"] = round(100 * prog["completed_units"] / prog["total_units"], 1)
            job["attempt"] = max(job.get("attempt", 0), ev.get("attempt") or 0)
            parent = job
        if ev.get("task_id"):
            tkey = (parent["id"], ev["task_id"])
            task = tasks.get(tkey)
            if task is None:
                task = tasks[tkey] = _node(f"{parent['id']}:{ev['task_id']}", "task", ev["task_id"], parent=parent["id"],
                                           status="pending", task_id=ev["task_id"])  # fmt: skip
                parent["children"].append(task)
            _touch(task, ev)
            if ev["type"].startswith("task_") or ev["type"] in ("scene_rendered", "reviewer_finished", "qa_failed"):
                task["status"] = ev["status"] if ev["status"] != "none" else task["status"]
                task["current_action"] = ev["message"]
                if ev["reason"]:
                    task["blocking_reason"] = ev["reason"]
            attempt = ev.get("attempt") or 0
            if attempt:
                marks = {a["id"]: a for a in task["children"]}
                aid = f"{task['id']}:a{attempt}"
                mark = marks.get(aid) or _node(
                    aid, "attempt", f"попытка {attempt}", parent=task["id"], status="running"
                )
                if aid not in marks:
                    task["children"].append(mark)
                _touch(mark, ev)
                if ev["status"] != "none":
                    mark["status"] = ev["status"]
    # Текущее действие: стадии из project.stages — точнее событий (процент и ETA живые).
    current_run = project.get("run_id")
    if current_run and current_run in runs and project.get("status") in ("queued", "running"):
        run = runs[current_run]
        by_stage = {n["stage_id"]: n for (pid, _), n in stage_nodes.items() if pid.startswith(current_run)}
        for s in project.get("stages") or []:
            node = by_stage.get(s["id"])
            if node is None:
                continue
            node["percent"] = s["percent"] if s["status"] in ("running", "pending") else node["percent"]
            node["status"] = (
                {"done": "completed", "canceled": "cancelled"}.get(s["status"], s["status"])
                if s["status"] != "pending"
                else node["status"]
            )
        run["eta"] = eta(project.get("stages") or [], stats, now)
    for node in list(runs.values()):
        _finalize(node, now)
    _counts(root)
    waiting = [r for r in runs.values() if r["status"] == "awaiting_approval"]
    if project.get("status") in ("queued", "running"):
        root["percent"] = None  # 100 % — только после проверки готового файла (§14.2)
    elif waiting and project.get("status") == "done":  # ролик готов, но запуск ревью ждёт человека
        root["status"], root["blocking_reason"] = "awaiting_approval", waiting[-1].get("blocking_reason")
        root["percent"] = None
    elif root["status"] == "completed":
        root["percent"] = 100.0
    root["last_updated"] = max((ev["occurred_at"] for ev in events_list), default=None)
    return root


def _finalize(node: dict, now: float) -> None:
    for kid in node["children"]:
        _finalize(kid, now)
    if (
        node["kind"] == "phase"
        and node["status"] == "running"
        and node["children"]
        and all(k["status"] in ("completed", "skipped") for k in node["children"])
    ):
        node["status"] = "completed"
    if node["started_at"]:
        end = node["finished_at"] or (
            now if node["status"] in ("running", "retrying", "queued") else node["updated_at"]
        )
        node["elapsed_s"] = round(max(0.0, (end or now) - node["started_at"]), 1)
    if node["status"] == "completed":
        node["percent"] = 100.0
    elif node["status"] in ("failed", "cancelled", "blocked", "skipped") and node["percent"] is None:
        node["percent"] = 0.0


def _project_status(project: dict) -> str:
    status = project.get("status")
    return {"queued": "queued", "running": "running", "done": "completed", "failed": "failed", "canceled": "cancelled",
            "review": "awaiting_approval", "waiting_approval": "awaiting_approval", "blocked": "blocked",
            "interrupted": "paused", "draft": "pending"}.get(status, "pending")  # fmt: skip


# ---------- ресурсы ----------


def _vm_stat() -> dict | None:
    """macOS vm_stat → занятая память (байты); None — нет команды."""
    try:
        out = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    page = re.search(r"page size of (\d+) bytes", out)
    size = int(page[1]) if page else 4096
    values = {}
    for line in out.splitlines():
        m = re.match(r"^(Pages (?:active|wired down|occupied by compressor|free|inactive|speculative)):\s+(\d+)", line)
        if m:
            values[m[1]] = int(m[2]) * size
    if not values:
        return None
    used = (
        values.get("Pages active", 0)
        + values.get("Pages wired down", 0)
        + values.get("Pages occupied by compressor", 0)
    )
    return {"used": used}


def resources(data_dir: Path, *, queue_depth: int, gpu_waiting: int, gpu_holder: str | None, last_job: dict | None,
              retries: int) -> dict:  # fmt: skip
    """Только измеримые метрики (§14.6); чего нет — status unavailable с причиной."""
    out: dict = {"sampled_at": round(time.time(), 3)}
    try:
        load1, load5, _ = os.getloadavg()
        out["cpu"] = {"load_1m": round(load1, 2), "load_5m": round(load5, 2), "cores": os.cpu_count()}
    except (OSError, AttributeError):
        out["cpu"] = {"status": "unavailable"}
    try:
        total = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
        vm = _vm_stat()
        out["ram"] = {"total_bytes": total, **({"used_bytes": vm["used"]} if vm else {"status": "partial"})}
    except (OSError, ValueError, AttributeError):
        out["ram"] = {"status": "unavailable"}
    try:
        usage = shutil.disk_usage(data_dir)
        out["disk"] = {"free_bytes": usage.free, "total_bytes": usage.total}
    except OSError:
        out["disk"] = {"status": "unavailable"}
    out["gpu"] = {"status": "unavailable", "reason": "на macOS без powermetrics (root) нет метрик GPU/VRAM; "
                                                     "GPU-замок: " + (gpu_holder or "свободен")}  # fmt: skip
    out["queue"] = {"depth": queue_depth, "gpu_waiting": gpu_waiting}
    out["render"] = {"fps": (last_job or {}).get("metrics", {}).get("render_fps"),
                     "speed": (last_job or {}).get("metrics", {}).get("speed"), "job_id": (last_job or {}).get("job_id")}  # fmt: skip
    out["retries"] = retries
    warnings = []
    if out["disk"].get("free_bytes") is not None and out["disk"]["free_bytes"] < 5 * 2**30:
        warnings.append("на диске данных меньше 5 ГБ")
    if out["cpu"].get("load_1m") and out["cpu"].get("cores") and out["cpu"]["load_1m"] > 2 * out["cpu"]["cores"]:
        warnings.append("процессор перегружен")
    out["warnings"] = warnings
    return out


def ffmpeg_metrics(line: str) -> dict | None:
    """fps= и speed= из строк `-progress` ffmpeg."""
    m = _FF_FPS.match(line.strip())
    if m:
        return {"render_fps": float(m[1])}
    m = _FF_SPEED.match(line.strip())
    if m:
        return {"speed": float(m[1])}
    return None


def new_run_id() -> str:
    return "run_" + uuid.uuid4().hex[:8]


def check(record: dict) -> list[str]:
    return schemas.validate("run_event", record)
