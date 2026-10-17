"""Бэкфилл журнала событий из того, что выпуски хранили до него (AN-002).

    python -m aiplatform.studio.backfill [--data <каталог данных>] [--dry-run]
    python -m aiplatform.studio.backfill --dimensions [--channels channels.json]  # AN-006, разово

Источники: project.json (создание, архив, последнее состояние шагов), лог
выпуска (▶ ✓ ✗ ⏸ ↷ ■ — история запусков, со временем без даты), published.json
(отправки, пакеты, снимки отклика). Все события — с `source: backfill`.

Повторный запуск не дублирует: event_id детерминированы (хэш от выпуска и
факта), уже записанные пропускаются. Живые события журнала не повторяются:
по выпуску берутся только факты раньше его первого живого события.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

from aiplatform.core import events, media
from aiplatform.studio import pipeline, publish, stats, steps

SOURCE = "backfill"
# Строка лога выпуска: «ЧЧ:ММ:СС» (до журнала) или «ГГГГ-ММ-ДД ЧЧ:ММ:СС», затем значок и шаг.
_LINE = re.compile(r"^(?:(\d{4}-\d{2}-\d{2}) )?(\d{2}):(\d{2}):(\d{2}) ([▶✓✗⏸↷■]) ([^:]+?)(?::.*)?$")
MARKS = {
    "▶": ("step.started", pipeline.RUNNING),
    "✓": ("step.finished", pipeline.DONE),
    "✗": ("step.failed", pipeline.FAILED),
    "⏸": ("step.waiting", pipeline.WAITING),
    "↷": ("step.skipped", pipeline.SKIPPED),
    "■": ("step.canceled", pipeline.PENDING),
}
STATE_EVENTS = {
    pipeline.DONE: "step.finished",
    pipeline.FAILED: "step.failed",
    pipeline.WAITING: "step.waiting",
    pipeline.SKIPPED: "step.skipped",
}
BY_LABEL = {label: step_id for step_id, label in pipeline.LABELS.items()}


def event_id(*parts: object) -> str:
    return "bf-" + hashlib.sha1("|".join(map(str, parts)).encode("utf-8")).hexdigest()[:20]


def log_events(project: dict) -> list[dict]:
    """События шагов из лога выпуска. У строк без даты день восстанавливается: от дня создания
    выпуска, время пошло назад — следующий день (props.approx_ts)."""
    out: list[dict] = []
    day = datetime.fromtimestamp(project.get("created_at") or 0).date()
    last: datetime | None = None
    seen: dict[str, int] = {}
    for line in project.get("log") or []:
        m = _LINE.match(line)
        if not m or m[6].strip() not in BY_LABEL:
            continue
        date_s, hh, mm, ss, mark, label = m.groups()
        if date_s:
            at = datetime.fromisoformat(f"{date_s} {hh}:{mm}:{ss}")
        else:
            at = datetime.combine(day, datetime.strptime(f"{hh}:{mm}:{ss}", "%H:%M:%S").time())
            while last is not None and at < last - timedelta(seconds=1):
                at += timedelta(days=1)
            day = at.date()
        last = at
        seen[line] = seen.get(line, 0) + 1
        event, status = MARKS[mark]
        out.append(
            {
                "event": event,
                "ts": at.timestamp(),
                "event_id": event_id(project["id"], "log", line, seen[line]),
                "step": BY_LABEL[label.strip()],
                "status_to": status,
                "props": None if date_s else {"approx_ts": True},
            }
        )
    return out


def state_events(project: dict, logged: set[str]) -> list[dict]:
    """Последнее состояние шагов из project.json — для шагов, о которых лог ничего не сохранил."""
    out = []
    for step_id, state in (project.get("steps") or {}).items():
        event = STATE_EVENTS.get(state.get("status"))
        if step_id in logged or event is None or not state.get("updated_at"):
            continue
        started = state.get("started_at")
        duration = state["updated_at"] - started if started else None
        out.append(
            {
                "event": event,
                "ts": state["updated_at"],
                "event_id": event_id(project["id"], "state", step_id, state["status"], state["updated_at"]),
                "step": step_id,
                "status_to": state["status"],
                "duration_s": duration,
                "attempt": state.get("attempt"),
            }
        )
    return out


def published_events(project: dict, records: list[dict]) -> list[dict]:
    """Публикации и снимки отклика из published.json."""
    out = []
    pid = project["id"]
    for record in records:
        channel = record["channel"]
        base = {"channel_id": channel, "publication_id": record.get("attempt_id"), "step": "publish"}
        state = record.get("state")
        if record.get("published_at") and record.get("external_url"):
            ts = record["published_at"]
            out.append(base | {"event": "publication.uploaded", "ts": ts, "event_id": event_id(pid, channel, "up", ts)})
        if record.get("files") and record.get("ok"):
            ts = record.get("at") or project.get("updated_at")
            out.append(base | {"event": "publication.packaged", "ts": ts, "event_id": event_id(pid, channel, "pkg")})
        elif record.get("sent") and (state in (None, steps.SENT)):
            ts = record["sent"]
            out.append(base | {"event": "publication.sent", "ts": ts, "event_id": event_id(pid, channel, "sent", ts)})
        elif state in (steps.SEND_FAILED, steps.UNKNOWN) and record.get("at"):
            ts = record["at"]
            out.append(
                base
                | {
                    "event": "publication.failed",
                    "ts": ts,
                    "event_id": event_id(pid, channel, "fail", ts),
                    "props": {"state": state},
                }
            )
        for snap in record.get("stats") or []:
            for metric in ("views", "members"):
                if snap.get(metric) is None or not snap.get("at"):
                    continue
                out.append(
                    base
                    | {
                        "event": "stats.snapshot",
                        "ts": snap["at"],
                        "event_id": event_id(pid, channel, "stat", metric, snap["at"]),
                        "metric": metric,
                        "value": snap[metric],
                        "age_h": snap.get("age_h"),
                        "props": {"manual": True} if snap.get("manual") else None,
                        "step": None,
                    }
                )
    return out


def project_events(project: dict, folder: Path) -> list[dict]:
    """Все факты одного выпуска как события (ещё без отсечки по живым)."""
    pid = project["id"]
    out = [{"event": "project.created", "ts": project.get("created_at"), "event_id": event_id(pid, "created")}]
    if project.get("archived"):
        out.append(
            {
                "event": "project.archived",
                "ts": project.get("updated_at"),
                "event_id": event_id(pid, "archived"),
                "props": {"archived": True},
            }
        )
    logged = log_events(project)
    out += logged
    out += state_events(project, {e["step"] for e in logged})
    out += published_events(project, steps.load_published(folder))
    return [e for e in out if e.get("ts")]


def run(data_dir: Path, *, dry_run: bool = False) -> dict:
    """Дописать в журнал недостающие события; вернуть счётчики."""
    log = events.EventLog(data_dir / events.FILE)
    existing = log.read()
    known = {e.get("event_id") for e in existing}
    first_live: dict[str, float] = {}
    for e in existing:
        if e.get("source") != SOURCE and e.get("project_id") and isinstance(e.get("ts"), (int, float)):
            first_live[e["project_id"]] = min(first_live.get(e["project_id"], e["ts"]), e["ts"])
    projects_dir = data_dir / "studio" / "projects"
    added = skipped = projects = 0
    for path in sorted(projects_dir.glob("*/project.json")):
        try:
            project = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(project, dict) or not project.get("id"):
            continue
        projects += 1
        cutoff = first_live.get(project["id"], float("inf"))
        found = sorted(project_events(project, path.parent), key=lambda e: e["ts"])
        for item in found:
            if item["event_id"] in known or item["ts"] >= cutoff:
                skipped += 1
                continue
            known.add(item["event_id"])
            added += 1
            if dry_run:
                continue
            fields = {k: v for k, v in item.items() if k not in ("event", "ts", "event_id")}
            log.emit(
                item["event"],
                actor=events.SYSTEM,
                ts=item["ts"],
                event_id=item["event_id"],
                project_id=project["id"],
                scenario=project.get("scenario"),
                source=SOURCE,
                **fields,
            )
    return {"projects": projects, "added": added, "skipped": skipped}


def record_dimensions(record: dict, folder: Path, formats: dict[str, str], ffmpeg: str) -> dict:
    """Атрибуты публикации (AN-006), которых нет в записи до него: формат — из записи или текущего
    конфига канала (format_guessed), media_s — по файлу выпуска (ffprobe), день и час — по времени выхода."""
    out: dict = {}
    fmt = record.get("format") or formats.get(record["channel"])
    if not record.get("format") and fmt:
        out |= {"format": fmt, "format_guessed": True}
    post_format = record.get("post_format") or ("photo" if record.get("parts") else "video")
    if "post_format" not in record:
        out["post_format"] = post_format
    if record.get("media_s") is None:
        if post_format == "photo":
            sent_file = steps.find(folder, "voice", steps.AUDIO_EXT)
        else:
            sent_file = folder / (steps.SHORT_VIDEO if fmt == steps.VERTICAL else steps.VIDEO)
        seconds = media.duration(sent_file, ffmpeg) if sent_file is not None and sent_file.is_file() else 0.0
        if seconds:
            out["media_s"] = round(seconds, 1)
    when = stats.sent_at(record)
    if "weekday" not in record and when is not None:
        out |= steps.when_fields(when)
    return out


def fill_dimensions(data_dir: Path, formats: dict[str, str], *, ffmpeg: str = "ffmpeg", dry_run: bool = False) -> dict:
    """Разово дописать атрибуты публикации в published.json выпусков. Запускать при остановленном
    сервисе: published.json переписывается целиком. Повторный запуск ничего не меняет."""
    projects = records_changed = 0
    for path in sorted((data_dir / "studio" / "projects").glob("*/" + steps.PUBLISHED)):
        records = steps.load_published(path.parent)
        fresh = [r | record_dimensions(r, path.parent, formats, ffmpeg) for r in records]
        changed = sum(a != b for a, b in zip(records, fresh, strict=True))
        if not changed:
            continue
        projects += 1
        records_changed += changed
        if not dry_run:
            steps.save_published(path.parent, fresh)
    return {"projects": projects, "records": records_changed}


def main(argv: list[str] | None = None) -> int:
    from aiplatform.config import SERVICE_DIR

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=str(SERVICE_DIR / "data"), help="каталог данных (AIP_DATA_DIR)")
    parser.add_argument("--dry-run", action="store_true", help="только посчитать")
    parser.add_argument(
        "--dimensions", action="store_true", help="дописать атрибуты публикаций (media_s и др., AN-006) вместо событий"
    )
    parser.add_argument("--channels", default=str(SERVICE_DIR / "channels.json"), help="channels.json (AIP_CHANNELS)")
    parser.add_argument("--ffmpeg", default="ffmpeg", help="ffmpeg (ffprobe — рядом)")
    args = parser.parse_args(argv)
    data_dir = Path(args.data).expanduser().resolve()
    if args.dimensions:
        try:
            formats = {c.id: c.format for c in publish.load_channels(Path(args.channels).expanduser())}
        except ValueError:
            formats = {}
        result = fill_dimensions(data_dir, formats, ffmpeg=args.ffmpeg, dry_run=args.dry_run)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    result = run(data_dir, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
