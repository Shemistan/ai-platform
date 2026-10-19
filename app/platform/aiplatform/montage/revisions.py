"""Ревизии проекта (MASTER_SPEC §7–8): снимки манифестов и восстановление.

`revisions/<n>.json` — копия манифестов (project.json без журнала, план, EDL, карта времени,
знания, биты, стиль, раскадровка, таймлайн, QA) с подписью и временем. Снимок делается после
каждого действия конвейера и ручной правки; `restore` возвращает манифесты (медиа и кэш сегментов
и сцен не трогает: рендер по хэшам возьмёт своё) и сам становится новым снимком. Хранится не
больше KEEP последних.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import schemas

DIR = "revisions"
KEEP = 50
FILES = ("project.json", "plan.json", "edl.json", "timemap.json", "documents.json", "knowledge.json", "beats.json",
         "style_bible.json", "storyboard.json", "timeline.json", "qa_report.json")  # fmt: skip
SKIP_PROJECT_KEYS = ("log",)


def _read(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _numbers(folder: Path) -> list[int]:
    root = folder / DIR
    return sorted(int(p.stem) for p in root.glob("*.json") if p.stem.isdigit()) if root.is_dir() else []


def snapshot(folder: Path, label: str) -> dict:
    """Снимок текущих манифестов; возвращает запись Revision."""
    data: dict[str, object] = {}
    for name in FILES:
        value = _read(folder / name)
        if value is None:
            continue
        if name == "project.json" and isinstance(value, dict):
            value = {k: v for k, v in value.items() if k not in SKIP_PROJECT_KEYS}
        data[name] = value
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True).encode("utf-8")
    numbers = _numbers(folder)
    n = (numbers[-1] if numbers else 0) + 1
    record = {"n": n, "at": round(time.time(), 3), "label": " ".join(label.split())[:120], "files": sorted(data),
              "project_hash": hashlib.sha256(raw).hexdigest()[:16]}  # fmt: skip
    schemas.check("revision", record)
    root = folder / DIR
    root.mkdir(parents=True, exist_ok=True)
    atomic_write(root / f"{n}.json", json.dumps({**record, "data": data}, ensure_ascii=False))
    for old in numbers[: max(0, len(numbers) + 1 - KEEP)]:
        (root / f"{old}.json").unlink(missing_ok=True)
    return record


def listing(folder: Path) -> list[dict]:
    out = []
    for n in _numbers(folder):
        value = _read(folder / DIR / f"{n}.json")
        if isinstance(value, dict):
            out.append({k: value[k] for k in ("n", "at", "label", "files", "project_hash") if k in value})
    return out


def restore(folder: Path, n: int) -> dict | None:
    """Вернуть манифесты снимка n; файлы, которых в снимке нет, убираются. Возвращает project.json снимка."""
    value = _read(folder / DIR / f"{n}.json")
    if not isinstance(value, dict) or not isinstance(value.get("data"), dict):
        return None
    data = value["data"]
    for name in FILES:
        if name in data:
            atomic_write(
                folder / name, json.dumps(data[name], ensure_ascii=False, indent=2 if name == "project.json" else None)
            )
        elif name != "project.json":
            (folder / name).unlink(missing_ok=True)
    return data.get("project.json")
