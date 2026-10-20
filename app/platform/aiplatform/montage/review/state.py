"""Запуск и раунды ревью (ITERATIVE_REVIEW_SPEC §9–10): state machine, чекпоинты, файлы.

`review/runs/<run_id>/run.json` — запуск; `rounds/<n>/round.json` — раунд со стадиями
analyze → consolidate → approve → repair → render → validate → compare. Переход — только из
TRANSITIONS; каждый переход пишется в `history` и на диск (`checkpoint`): после рестарта
запуск читается с последнего чекпоинта. Активный запуск при загрузке → `paused` с причиной
(воркер его не продолжит сам: человек нажимает «Продолжить», готовые артефакты стадий
пропускаются — ничего не пересчитывается зря).
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import schemas

DIR = "review"
RUNS = "runs"
STAGES = ("analyze", "consolidate", "approve", "repair", "render", "validate", "compare")
ACTIVE = ("preparing", "reviewing", "consolidating", "repairing", "rendering", "validating", "comparing")
WAITING = ("awaiting_approval", "awaiting_decision")
FINAL = ("completed", "cancelled", "failed")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "idle": ("preparing", "cancelled", "blocked"),
    "preparing": ("reviewing", "paused", "failed", "blocked", "cancelled"),
    "reviewing": ("consolidating", "paused", "failed", "blocked", "cancelled"),
    "consolidating": ("awaiting_approval", "repairing", "completed", "paused", "failed", "blocked", "cancelled"),
    "awaiting_approval": ("repairing", "completed", "cancelled", "blocked", "paused"),
    "repairing": ("rendering", "paused", "failed", "blocked", "cancelled", "completed"),
    "rendering": ("validating", "paused", "failed", "blocked", "cancelled"),
    "validating": ("comparing", "rejected", "paused", "failed", "blocked", "cancelled"),
    "comparing": ("accepted", "rejected", "awaiting_decision", "paused", "failed", "blocked", "cancelled"),
    "awaiting_decision": ("accepted", "rejected", "cancelled", "blocked", "paused"),
    "accepted": ("preparing", "completed", "cancelled", "paused"),
    "rejected": ("preparing", "completed", "cancelled", "paused"),
    "paused": ("preparing", "reviewing", "consolidating", "awaiting_approval", "repairing", "rendering",
               "validating", "comparing", "awaiting_decision", "accepted", "rejected", "cancelled", "completed"),
    "blocked": ("preparing", "cancelled", "completed"),
    "completed": (),
    "cancelled": (),
    "failed": ("preparing", "cancelled"),
}  # fmt: skip
INTERRUPTED = "Сервис был перезапущен во время раунда — нажми «Продолжить»: готовые стадии не пересчитываются."
_ID = "rv_"


class StateError(ValueError):
    """Переход не по машине состояний или запуск не там, где ждали."""


def root(folder: Path) -> Path:
    return folder / DIR / RUNS


def run_dir(folder: Path, run_id: str) -> Path:
    return root(folder) / run_id


def round_dir(folder: Path, run_id: str, n: int) -> Path:
    return run_dir(folder, run_id) / "rounds" / str(n)


def round_id(n: int) -> str:
    return f"round-{n:03d}"


def new_run(project_id: str, policy: dict, *, lock: dict, capabilities: dict | None = None) -> dict:
    now = round(time.time(), 3)
    run = {
        "run_id": _ID + uuid.uuid4().hex[:8],
        "project_id": project_id,
        "schema_version": schemas.SCHEMA_VERSION,
        "state": "idle",
        "policy": policy,
        "round": 0,
        "rounds": [],
        "versions": ["V0"],
        "accepted_version": "V0",
        "selected_version": None,
        "budget": {"gpu_s": 0.0, "wall_s": 0.0, "new_assets": 0, "llm_calls": 0},
        "stop_reason": None,
        "blocking_reason": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
        "lock": lock,
        "checkpoint": {"state": "idle", "at": now, "round": 0},
        "history": [],
        "capabilities": capabilities or {},
    }
    schemas.check("review_run", run)
    return run


def can(run: dict, state: str) -> bool:
    return state in TRANSITIONS.get(run["state"], ())


def advance(run: dict, state: str, *, reason: str = "", **fields) -> dict:
    """Перевести запуск в state (по TRANSITIONS), записать историю и чекпоинт в памяти."""
    if not can(run, state):
        raise StateError(f"Из «{run['state']}» нельзя в «{state}».")
    now = round(time.time(), 3)
    run["history"].append({"at": now, "from": run["state"], "to": state, "reason": reason})
    del run["history"][:-200]
    run["state"] = state
    run["updated_at"] = now
    if state == "preparing" and run["started_at"] is None:
        run["started_at"] = now
    if state in FINAL:
        run["finished_at"] = now
    if state != "paused":
        run["checkpoint"] = {"state": state, "at": now, "round": run["round"]}
    for key, value in fields.items():
        run[key] = value
    return run


def checkpoint(folder: Path, run: dict) -> None:
    """Записать запуск на диск (после каждой стадии — §9)."""
    schemas.check("review_run", run)
    target = run_dir(folder, run["run_id"])
    target.mkdir(parents=True, exist_ok=True)
    atomic_write(target / "run.json", json.dumps(run, ensure_ascii=False, indent=1))


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def load_run(folder: Path, run_id: str) -> dict | None:
    return _read(run_dir(folder, run_id) / "run.json")


def list_runs(folder: Path) -> list[dict]:
    base = root(folder)
    out = [r for p in base.glob("*/run.json") if (r := _read(p))] if base.is_dir() else []
    return sorted(out, key=lambda r: r["created_at"])


def interrupted(folder: Path) -> list[dict]:
    """Старт сервиса: активные запуски → paused с причиной (ничего не продолжаем сами)."""
    out = []
    for run in list_runs(folder):
        if run["state"] in ACTIVE:
            advance(run, "paused", reason="interrupted", blocking_reason=INTERRUPTED)
            checkpoint(folder, run)
            out.append(run)
    return out


def new_round(run: dict, base_version: str) -> dict:
    n = run["round"] + 1
    now = round(time.time(), 3)
    item = {
        "n": n,
        "round_id": round_id(n),
        "base_version": base_version,
        "candidate_version": None,
        "state": "preparing",
        "stages": [
            {"id": s, "status": "pending", "started_at": None, "finished_at": None, "detail": ""} for s in STAGES
        ],  # fmt: skip
        "counts": {k: 0 for k in ("open", "accepted", "rejected", "deferred", "fixed", "failed", "invalid", "total")},
        "scenes_touched": [],
        "frames_changed": [],
        "qa_delta": {},
        "decision": None,
        "decision_reason": "",
        "started_at": now,
        "finished_at": None,
        "attempt": 1,
        "job_ids": [],
    }
    schemas.check("review_round", item)
    return item


def round_stage(item: dict, stage_id: str, status: str, detail: str = "") -> None:
    """Стадия раунда: running закрывает предыдущие running как done; awaiting — ждёт человека."""
    now = round(time.time(), 3)
    for stage in item["stages"]:
        if stage["id"] == stage_id:
            if status == "running" and stage["status"] != "running":
                stage["started_at"] = now
            if status in ("done", "failed", "skipped", "blocked"):
                stage["started_at"] = stage["started_at"] or now
                stage["finished_at"] = now
            stage["status"] = status
            stage["detail"] = detail
        elif status == "running" and stage["status"] == "running":
            stage.update(status="done", finished_at=now)


def save_round(folder: Path, run: dict, item: dict) -> None:
    schemas.check("review_round", item)
    target = round_dir(folder, run["run_id"], item["n"])
    target.mkdir(parents=True, exist_ok=True)
    atomic_write(target / "round.json", json.dumps(item, ensure_ascii=False, indent=1))
    keys = ("n", "round_id", "base_version", "candidate_version", "state", "counts", "scenes_touched", "decision",
            "decision_reason", "started_at", "finished_at")  # fmt: skip
    summary = {k: item[k] for k in keys}
    summary["stages"] = [{"id": s["id"], "status": s["status"]} for s in item["stages"]]
    rounds = [r for r in run["rounds"] if r["n"] != item["n"]]
    run["rounds"] = sorted([*rounds, summary], key=lambda r: r["n"])


def load_round(folder: Path, run_id: str, n: int) -> dict | None:
    return _read(round_dir(folder, run_id, n) / "round.json")


def read_json(folder: Path, run_id: str, n: int, name: str) -> dict | None:
    return _read(round_dir(folder, run_id, n) / name)


def write_json(folder: Path, run_id: str, n: int, name: str, data: dict) -> None:
    target = round_dir(folder, run_id, n)
    target.mkdir(parents=True, exist_ok=True)
    atomic_write(target / name, json.dumps(data, ensure_ascii=False, indent=1))


def public(run: dict) -> dict:
    """Запуск для UI: без тяжёлой истории, с текущим раундом и тем, что ждёт человека."""
    out = {k: v for k, v in run.items() if k != "history"}
    out["history"] = run["history"][-20:]
    out["waiting"] = run["state"] in WAITING
    out["active"] = run["state"] in ACTIVE
    out["final"] = run["state"] in FINAL
    out["rounds_planned"] = run["policy"]["max_rounds"]
    out["rounds_done"] = sum(1 for r in run["rounds"] if r.get("decision"))
    return out
