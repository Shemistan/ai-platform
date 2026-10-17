"""Уборка данных: сколько места занимает каждая область и удаление старого, но восстановимого.

Удаляется только то, что выпуски уже скопировали к себе или что можно
сделать заново: завершённые задачи вкладок «Фото», «Видео», «Аудио», записи
кэша генераций, к которым давно не обращались, загрузки и выпуски из архива
(старше `days`). Загрузка (фото людей, записи голоса) удаляется, только если на неё
не ссылается ни одна оставшаяся задача вкладки или выпуск (CMP-016). Опубликованные выпуски (шаг «Публикация» готов):

- через `trim_days` после публикации — без тяжёлых промежуточных файлов
  (картинки слайдов, исходники клипов, файлы шортса, озвучка в другом
  темпе); `video.mp4`, `short.mp4`, `project.json`, `published.json`, тексты
  и постер остаются;
- через `archive_days` — в архив сами. Такой выпуск (`archived_by:
  cleanup`) уборка потом не удаляет: в нём история публикаций.

Идущие задачи и выпуски не трогаются никогда; неопубликованные неархивные — тоже.
Если архивный выпуск всё же удаляется (в архив его убрал человек), его
project.json и published.json остаются в архиве аналитики (studio/history.py, AN-008).
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

from aiplatform.core import log

DAY_S = 86400
JOB_TABS = {"photo": "Задачи «Фото»", "video": "Задачи «Видео»", "audio": "Задачи «Аудио»"}
ACTIVE = ("queued", "running")
# Тяжёлые промежуточные файлы опубликованного выпуска: каталоги и шаблоны файлов (steps.py).
TRIM_DIRS = ("images", "clips", "short")
TRIM_GLOBS = ("voice-x*.mp3", "*.tmp")
_UPLOAD_ID = re.compile(r"[0-9a-f]{32}")  # id загрузки (core/uploads.py) в записи задачи или выпуска
AUTO_ARCHIVED = "cleanup"  # project["archived_by"]: выпуск убрала в архив уборка


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

    def run(self, days: int, *, dry: bool, now: float | None = None, policy: dict | None = None) -> dict:
        """Удалить (или с dry — только посчитать) всё восстановимое старше days дней.

        policy — раздел cleanup настроек (trim_days, archive_days); не задан — из settings.json."""
        now = now or time.time()
        cutoff = now - days * DAY_S
        if policy is None:
            policy = self.ctx.config.settings.load()["cleanup"]
        found = {"jobs": 0, "cache": 0, "archived": 0, "uploads": 0, "trimmed": 0, "auto_archived": 0, "bytes": 0}
        used: set[str] = set()  # id загрузок, на которые ссылаются оставшиеся задачи и выпуски
        for tab in JOB_TABS:
            module = self.ctx.modules.get(tab)
            if module is None:
                continue
            for job in module.jobs.list():
                stamp = job.get("finished_at") or job.get("created_at") or 0
                if job.get("status") in ACTIVE or stamp >= cutoff:
                    used |= upload_ids(job)
                    continue
                found["jobs"] += 1
                folder = self.ctx.config.data_dir / tab / "jobs" / job["id"]
                found["bytes"] += size_of(folder) if dry else module.jobs.forget(job["id"])
        uploads = self.ctx.config.uploads_dir
        studio = self.studio
        if studio is not None:
            for project in studio.studio.list(archived=False) + studio.studio.list(archived=True):
                used |= upload_ids(studio.studio.get(project["id"]) or project)
        for entry in uploads.iterdir() if uploads.is_dir() else []:
            if entry.is_file() and entry.stat().st_mtime < cutoff and entry.stem not in used:
                found["uploads"] += 1
                found["bytes"] += entry.stat().st_size
                if not dry:
                    entry.unlink(missing_ok=True)
        if studio is not None:
            cache = studio.config.cache_dir
            for entry in cache.iterdir() if cache.is_dir() else []:
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    found["cache"] += 1
                    found["bytes"] += entry.stat().st_size
                    if not dry:
                        entry.unlink(missing_ok=True)
            self._published(studio.studio, found, now, policy, dry=dry)
            for project in studio.studio.list(archived=True):
                if project["running"] or project["updated_at"] >= cutoff:
                    continue
                full = studio.studio.get(project["id"]) or {}
                if full.get("archived_by") == AUTO_ARCHIVED:
                    continue  # убран в архив уборкой: история публикаций остаётся
                found["archived"] += 1
                folder = studio.studio.project_dir(project["id"])
                found["bytes"] += size_of(folder) if dry else studio.studio.delete(project["id"])
        if not dry:
            log.event("cleanup.done", days=days, **found)
        return found

    def _published(self, studio, found: dict, now: float, policy: dict, *, dry: bool) -> None:
        """Опубликованные выпуски: через trim_days — без промежуточных файлов, через archive_days — в архив."""
        trim_days, archive_days = policy.get("trim_days") or 0, policy.get("archive_days") or 0
        if not trim_days and not archive_days:
            return
        for item in studio.list(archived=False) + studio.list(archived=True):
            project = studio.get(item["id"])
            if project is None or project.get("running"):
                continue
            publish = project.get("steps", {}).get("publish", {})
            if publish.get("status") != "done":
                continue
            age_days = (now - (publish.get("updated_at") or project.get("updated_at") or now)) / DAY_S
            if trim_days and age_days >= trim_days:
                freed = trim(studio.project_dir(project["id"]), dry=dry)
                if freed:
                    found["trimmed"] += 1
                    found["bytes"] += freed
                    if not dry:
                        studio.annotate(project["id"], trimmed_at=now)
            if archive_days and age_days >= archive_days and not project.get("archived"):
                found["auto_archived"] += 1
                if not dry:
                    studio.archive(project["id"], True)
                    studio.annotate(project["id"], archived_by=AUTO_ARCHIVED)


def upload_ids(record: dict) -> set[str]:
    """Id загрузок, упомянутых в записи задачи или выпуска (source_url, upload_id, keyframes…)."""
    return set(_UPLOAD_ID.findall(json.dumps(record, ensure_ascii=False, default=str)))


def trim(folder: Path, *, dry: bool) -> int:
    """Удалить тяжёлые промежуточные файлы выпуска (TRIM_DIRS, TRIM_GLOBS); вернуть байты."""
    freed = 0
    for name in TRIM_DIRS:
        path = folder / name
        if path.is_dir():
            freed += size_of(path) if dry else remove_tree(path)
    for pattern in TRIM_GLOBS:
        for path in folder.glob(pattern):
            if path.is_file():
                freed += path.stat().st_size
                if not dry:
                    path.unlink(missing_ok=True)
    return freed
