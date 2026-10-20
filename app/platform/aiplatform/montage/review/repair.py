"""Исполнение плана исправлений (ITERATIVE_REVIEW_SPEC §7): правки манифестов проекта под пересборку.

Действия идут в порядке DAG. Сценные — через `storyboard.edit`: новый хэш только у правленой сцены,
остальные при сборке берутся из кэша. `caption_words` — настройка проекта (куски субтитров короче,
пересчёт при сборке). `cut_words` — вырезка в плане + `_write_edl`: TimeMap, якоря сцен и субтитры
пересчитываются (раскадровка не сбрасывается — только перепривязывается при сборке). Сборка —
`MontageProjects._render` (сегменты и сцены по кэшу, финал, QA). Попытка не удалась → ещё одна в пределах
`max_render_attempts_per_issue`, потом действия `failed`.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from aiplatform.core import runner
from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import edl, storyboard
from aiplatform.montage.review import planner


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def apply(projects, project: dict, plan_data: dict, *, fps: int, total_frames: int) -> list[dict]:
    """Применить утверждённые действия к манифестам проекта; вернуть список применённых (status done/failed)."""
    pid = project["id"]
    folder = projects.dir(pid)
    board = _read(folder / "storyboard.json")
    scenes = list(board.get("scenes") or [])
    by_id = {s["id"]: s for s in scenes}
    touched: list[dict] = []
    words = (_read(folder / "words.json")).get("words") or []
    plan_file = _read(folder / "plan.json")
    cuts_changed = False
    board_changed = False
    for action in planner.ready(plan_data):
        action["attempts"] += 1
        try:
            if action["op"].startswith("scene_"):
                scene = by_id.get(action["scene_id"] or "")
                if scene is None:
                    raise ValueError(f"сцены {action['scene_id']} нет в раскадровке")
                patch = planner.scene_patch(action, scene, fps, total_frames)
                new = storyboard.edit(scene, patch, total_frames)
                by_id[scene["id"]] = new
                scenes = [new if s["id"] == scene["id"] else s for s in scenes]
                action["asset_hash_after"] = new["hash"]
                board_changed = True
            elif action["op"] == "caption_words":
                project["options"]["caption_words"] = int(action["params"].get("max_words") or 2)
            elif action["op"] == "cut_words":
                cut = {"from": action["params"]["from"], "to": action["params"]["to"], "kind": "review",
                       "reason": "ревью: " + (action.get("test") or "")[:80]}  # fmt: skip
                cuts = edl.clean_cuts([*(plan_file.get("cuts") or []), cut], len(words), default_kind="review")
                plan_file["cuts"] = cuts
                cuts_changed = True
            else:
                raise ValueError(f"неизвестное действие {action['op']}")
        except (storyboard.EditError, ValueError) as exc:
            action["status"], action["error"] = "failed", str(exc)[:300]
            touched.append(action)
            continue
        action["status"], action["error"] = "done", None
        touched.append(action)
    if board_changed:
        projects._write_storyboard(project, scenes, source="review")
    if cuts_changed:
        plan_file["edited_at"] = time.time()
        atomic_write(folder / "plan.json", json.dumps(plan_file, ensure_ascii=False))
        layout = plan_file.get("layout") or [{"from": 0, "layout": "head"}]
        projects._write_edl(project, words, plan_file["cuts"], layout)
        with projects._lock:
            projects._set_plan_summary(project, words, plan_file["cuts"], layout)
    with projects._lock:
        projects._touch(project)
    return touched


def render(projects, project: dict, cancel: threading.Event, *, attempts_left: int) -> tuple[bool, str]:
    """Пересборка проекта; (успех, ошибка). Повтор в пределах attempts_left при RunFailed."""
    error = ""
    for _ in range(max(1, attempts_left)):
        try:
            projects._render(project, cancel)
            return True, ""
        except runner.RunFailed as exc:
            error = str(exc)
    return False, error


def scenes_rerendered(before: dict[str, str], after: list[dict]) -> list[str]:
    """Сцены, у которых хэш изменился и которые отрисованы заново — новые ассеты (бюджет §2)."""
    return [
        s["id"]
        for s in after
        if s.get("enabled", True) and s["status"] == "rendered" and before.get(s["id"]) != s["hash"]
    ]
