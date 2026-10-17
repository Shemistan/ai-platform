"""Уборка данных: сколько места занимает каждая область и удаление старого, но восстановимого.

Удаляется только то, что выпуски уже скопировали к себе или что можно
сделать заново: завершённые задачи вкладок «Фото», «Видео», «Аудио», записи
кэша генераций, к которым давно не обращались, и выпуски из архива. Идущие
задачи и неархивные выпуски не трогаются никогда. История публикаций
удаляемого выпуска остаётся в архиве аналитики (studio/history.py, AN-008).
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from aiplatform.core import log

DAY_S = 86400
JOB_TABS = {"photo": "Задачи «Фото»", "video": "Задачи «Видео»", "audio": "Задачи «Аудио»"}
ACTIVE = ("queued", "running")


def size_of(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    if not path.is_dir():
        return 0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def remove_tree(path: Path) -> int:
    """Удалить каталог; вернуть, сколько байт он занимал."""
    freed = size_of(path)
    shutil.rmtree(path, ignore_errors=True)
    return freed


class Cleaner:
    def __init__(self, ctx) -> None:
        self.ctx = ctx  # server.Context: модули вкладок и сценариев

    @property
    def studio(self):
        return self.ctx.modules.get("studio")

    def usage(self) -> list[dict]:
        """Сколько места у каждой области данных."""
        data = self.ctx.config.data_dir
        out = [{"id": tab, "label": label, "bytes": size_of(data / tab)} for tab, label in JOB_TABS.items()]
        studio = self.studio
        if studio is not None:
            archived = {p["id"] for p in studio.studio.list(archived=True)}
            projects = studio.config.projects_dir
            sizes = {p.name: size_of(p) for p in projects.iterdir() if p.is_dir()} if projects.is_dir() else {}
            kept = sum(v for k, v in sizes.items() if k not in archived)
            out += [
                {"id": "projects", "label": "Выпуски", "bytes": kept},
                {"id": "archived", "label": "Выпуски в архиве", "bytes": sum(sizes.values()) - kept},
                {"id": "cache", "label": "Кэш генераций", "bytes": size_of(studio.config.cache_dir)},
            ]
        out.append({"id": "uploads", "label": "Загрузки", "bytes": size_of(data / "uploads")})
        return out

    def run(self, days: int, *, dry: bool, now: float | None = None) -> dict:
        """Удалить (или с dry — только посчитать) всё восстановимое старше days дней."""
        cutoff = (now or time.time()) - days * DAY_S
        found = {"jobs": 0, "cache": 0, "archived": 0, "bytes": 0}
        for tab in JOB_TABS:
            module = self.ctx.modules.get(tab)
            if module is None:
                continue
            for job in module.jobs.list():
                stamp = job.get("finished_at") or job.get("created_at") or 0
                if job.get("status") in ACTIVE or stamp >= cutoff:
                    continue
                found["jobs"] += 1
                folder = self.ctx.config.data_dir / tab / "jobs" / job["id"]
                found["bytes"] += size_of(folder) if dry else module.jobs.forget(job["id"])
        studio = self.studio
        if studio is not None:
            cache = studio.config.cache_dir
            for entry in cache.iterdir() if cache.is_dir() else []:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    found["cache"] += 1
                    found["bytes"] += entry.stat().st_size
                    if not dry:
                        entry.unlink(missing_ok=True)
            for project in studio.studio.list(archived=True):
                if project["running"] or project["updated_at"] >= cutoff:
                    continue
                found["archived"] += 1
                folder = studio.studio.project_dir(project["id"])
                found["bytes"] += size_of(folder) if dry else studio.studio.delete(project["id"])
        if not dry:
            log.event("cleanup.done", days=days, **found)
        return found
