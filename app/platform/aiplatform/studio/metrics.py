"""SLI выпусков по журналу событий (REL-010 поверх AN-002): блок «Здоровье».

Отдельного файла запусков нет: история шагов уже пишется append-only в
`<data>/events.jsonl` (core/events.py) — `step.finished` / `step.failed` с
`duration_s`, `publication.*`, `project.created` (actor=scheduler — выпуск по
расписанию). Уборка журнал не трогает, бэкап кладёт в архив.

`sli(events, days)` за последние days дней:

- доля упавших шагов — `step.failed` / (`step.finished` + `step.failed`), всего
  и у публикации;
- p50 / p90 сборки видео — `duration_s` у `step.finished` шага `render`;
- доля дней с дайджестом вовремя — дни, когда выпуск дайджеста, созданный в
  этот день, ушёл в канал (`publication.sent` / `packaged` / `uploaded`) до
  AIP_DIGEST_DEADLINE (12:00); дни считаются от первого события в окне, чтобы
  новая установка не выглядела провальной.

`GET /api/studio/health/stats` — окна 7 и 28 дней; их показывает «Настройки» → «Здоровье».
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable
from datetime import date, datetime, timedelta

WINDOWS = (7, 28)
DEFAULT_DEADLINE = "12:00"
DAY_S = 86400
SENT = ("publication.sent", "publication.packaged", "publication.uploaded", "publication.republished")


def percentile(values: list[float], q: float) -> float | None:
    """q-перцентиль (0..1) методом ближайшего ранга; пусто — None."""
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(1, math.ceil(q * len(ordered))) - 1], 1)


def _share(part: int, whole: int) -> float | None:
    return round(part / whole, 3) if whole else None


def _deadline(day: date, deadline: str) -> float:
    hour, minute = (int(x) for x in deadline.split(":"))
    return datetime(day.year, day.month, day.day, hour, minute).timestamp()


def _day(ts: float) -> date:
    return datetime.fromtimestamp(ts).date()


def sli(events: Iterable[dict], days: int, *, now: float | None = None, deadline: str = DEFAULT_DEADLINE) -> dict:
    now = now or time.time()
    since = now - days * DAY_S
    window = [e for e in events if isinstance(e.get("ts"), int | float) and since <= e["ts"] <= now]
    steps = [e for e in window if e.get("event") in ("step.finished", "step.failed")]
    failed = [e for e in steps if e["event"] == "step.failed"]
    publish = [e for e in steps if e.get("step") == "publish"]
    builds = [
        e["duration_s"]
        for e in steps
        if e["event"] == "step.finished" and e.get("step") == "render" and isinstance(e.get("duration_s"), int | float)
    ]
    # Дайджесты: создан в день D (по расписанию или руками) — ушёл в канал до дедлайна дня D.
    created = {
        e["project_id"]: e
        for e in window
        if e.get("event") == "project.created" and e.get("scenario") == "digest" and e.get("project_id")
    }
    first_sent: dict[str, float] = {}
    for e in window:
        pid = e.get("project_id")
        if e.get("event") in SENT and pid in created:
            first_sent[pid] = min(first_sent.get(pid, e["ts"]), e["ts"])
    on_time = {
        _day(created[pid]["ts"])
        for pid, ts in first_sent.items()
        if ts <= _deadline(_day(created[pid]["ts"]), deadline)
    }
    today = _day(now)
    start = max(min((_day(e["ts"]) for e in window), default=today), today - timedelta(days=days - 1))
    all_days = {start + timedelta(days=n) for n in range((today - start).days + 1)}
    failed_by_step: dict[str, int] = {}
    for e in failed:
        failed_by_step[e.get("step") or "?"] = failed_by_step.get(e.get("step") or "?", 0) + 1
    return {
        "days": days,
        "steps": len(steps),
        "failed_steps": len(failed),
        "failed_share": _share(len(failed), len(steps)),
        "publish_steps": len(publish),
        "publish_failed_share": _share(sum(e["event"] == "step.failed" for e in publish), len(publish)),
        "render_builds": len(builds),
        "render_p50_s": percentile(builds, 0.5),
        "render_p90_s": percentile(builds, 0.9),
        "digest_days": len(all_days),
        "digest_on_time_days": len(on_time & all_days),
        "digest_on_time_share": _share(len(on_time & all_days), len(all_days)),
        "scheduled_digests": sum(e.get("actor") == "scheduler" for e in created.values()),
        "failed_by_step": dict(sorted(failed_by_step.items(), key=lambda kv: -kv[1])),
    }


def report(events: list[dict], *, now: float | None = None, deadline: str = DEFAULT_DEADLINE) -> dict:
    """Окна 7 и 28 дней одним ответом."""
    now = now or time.time()
    return {"deadline": deadline, "windows": {str(d): sli(events, d, now=now, deadline=deadline) for d in WINDOWS}}
