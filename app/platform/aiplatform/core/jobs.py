"""Общее у очередей задач вкладок (BE-007): выборка списка задач `?limit=&since=`.

Первый шаг к одной очереди core.jobs вместо копий в photo/video/audio/core.tasks
(план — docs/superpowers/plans/2026-10-17-job-queue.md).
"""

from __future__ import annotations

from collections.abc import Iterable

from aiplatform.core.http import Request, bad_request

ACTIVE = ("queued", "running")
DEFAULT_LIMIT = 100  # столько задач отдаёт GET …/jobs без ?limit: опрос раз в секунду не тащит всю историю
MAX_LIMIT = 1000


def touched(job: dict) -> float:
    """Когда задача менялась последний раз: создана, запущена или закончена."""
    return max(float(job.get(k) or 0) for k in ("created_at", "started_at", "finished_at"))


def select(jobs: Iterable[dict], limit: int | None = None, since: float | None = None) -> list[dict]:
    """Новые сверху. since — только тронутые позже (идущие — всегда); limit — не больше N."""
    out = sorted(jobs, key=lambda j: j["created_at"], reverse=True)
    if since is not None:
        out = [j for j in out if j.get("status") in ACTIVE or touched(j) > since]
    return out if limit is None else out[:limit]


def list_args(req: Request) -> tuple[int, float | None]:
    """?limit=&since= из запроса списка; неверные — 400."""
    raw_limit, raw_since = req.arg("limit"), req.arg("since")
    try:
        limit = DEFAULT_LIMIT if raw_limit is None else int(raw_limit)
        since = None if raw_since is None else float(raw_since)
    except ValueError as exc:
        raise bad_request("limit — целое число, since — время unix.") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise bad_request(f"limit — от 1 до {MAX_LIMIT}.")
    return limit, since
