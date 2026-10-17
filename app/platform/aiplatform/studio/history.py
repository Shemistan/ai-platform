"""Архив аналитики: публикации выпусков, удалённых уборкой (AN-008).

Уборка удаляет архивный выпуск целиком — с медиа, ради которых она и нужна. История
публикаций весит килобайты и нужна «Что работает» и приросту подписчиков канала, поэтому
перед удалением project.json (без лога) и published.json выпуска дописываются строкой в
`data/analytics/publications.jsonl`. Повторная запись того же выпуска не дублируется.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

FILE = Path("analytics") / "publications.jsonl"


def load(path: Path) -> list[dict]:
    """Строки архива: {project_id, deleted_at, project, published}; битые строки пропускаются."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict) and isinstance(row.get("project"), dict) and isinstance(row.get("published"), list):
            row["published"] = [r for r in row["published"] if isinstance(r, dict) and r.get("channel")]
            out.append(row)
    return out


def keep(path: Path, project: dict, published: list[dict], now: float | None = None) -> bool:
    """Дописать выпуск в архив; уже записанный — не повторять. True — если дописан."""
    if any(row.get("project_id") == project.get("id") for row in load(path)):
        return False
    row = {
        "project_id": project.get("id"),
        "deleted_at": time.time() if now is None else now,
        "project": {k: v for k, v in project.items() if k != "log"},
        "published": published,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True
