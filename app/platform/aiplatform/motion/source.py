"""Готовый ролик «Монтажа» как исходник «Моушна»: без копии и без второй расшифровки.

Видео — жёсткая ссылка на output.mp4 (тот же диск; иначе копия). Слова — монтажная расшифровка
исходника, переложенная на время результата тем же листом монтажа (edl.build) с сеткой кадров
рендера, как его склеил «Монтаж».
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from aiplatform.montage import edl, render


def _read(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def finished(projects_dir: Path) -> list[dict]:
    """Ролики «Монтажа», которые можно взять: готовые, с output.mp4 и расшифровкой."""
    out = []
    for path in projects_dir.glob("*/project.json"):
        project = _read(path, {})
        folder = path.parent
        if not isinstance(project, dict) or project.get("status") != "done":
            continue
        if not (folder / "output.mp4").is_file() or not (folder / "words.json").is_file():
            continue
        output = project.get("output") or {}
        out.append({"id": folder.name, "title": project.get("title") or folder.name,
                    "duration": output.get("duration"), "updated_at": project.get("updated_at") or 0})  # fmt: skip
    return sorted(out, key=lambda p: p["updated_at"], reverse=True)


def output_words(folder: Path) -> list[dict]:
    """Слова результата монтажа во времени output.mp4: сегменты на сетке кадров, как в _assemble."""
    project = _read(folder / "project.json", {})
    words = (_read(folder / "words.json", {}) or {}).get("words") or []
    data = _read(folder / "plan.json", {}) or {}
    head = (project.get("sources") or {}).get("head") or {}
    silences = [tuple(s) for s in _read(folder / "silences.json", []) or []]
    max_pause = (project.get("options") or {}).get("max_pause", edl.MAX_PAUSE_S)
    layout = data.get("layout") or [{"from": 0, "layout": "head"}]
    result = edl.build(words, head.get("duration") or 0.0, data.get("cuts") or [], layout, silences,
                       max_pause=max_pause)  # fmt: skip
    out = []
    pos = 0.0
    for seg in result.segments:
        start, end = render.frame(seg.start), render.frame(seg.end)
        for i in range(seg.first, seg.last + 1):
            w = words[i]
            s = max(w["s"], start) - start + pos
            e = min(w["e"], end) - start + pos
            out.append({"w": w["w"], "s": round(s, 3), "e": round(max(e, s), 3)})
        pos += end - start
    return out


def link(src: Path, target: Path) -> None:
    """Жёсткая ссылка (мгновенно и без места), с другого диска — копия."""
    target.unlink(missing_ok=True)
    try:
        os.link(src, target)
    except OSError:
        shutil.copy2(src, target)
