"""Уборка данных: сколько места занимает каждая область и удаление старого, но восстановимого.

Удаляется только то, что выпуски уже скопировали к себе или что можно
сделать заново: завершённые задачи вкладок «Фото», «Видео», «Аудио», записи
кэша генераций, к которым давно не обращались, загрузки и выпуски из архива
(старше `days`). Опубликованные выпуски (шаг «Публикация» готов):

- через `trim_days` после публикации — без тяжёлых промежуточных файлов
  (картинки слайдов, исходники клипов, файлы шортса, озвучка в другом
  темпе); `video.mp4`, `short.mp4`, `project.json`, `published.json`, тексты
  и постер остаются;
- через `archive_days` — в архив сами. Такой выпуск (`archived_by:
  cleanup`) уборка потом не удаляет: в нём история публикаций.

Идущие задачи и выпуски не трогаются никогда; неопубликованные неархивные — тоже,
кроме брошенных: без движения дольше `draft_days` они уходят в архив (archive_stale,
ежедневно, с уведомлением; PRD-004).
Если архивный выпуск всё же удаляется (в архив его убрал человек), его
project.json и published.json остаются в архиве аналитики (studio/history.py, AN-008).
"""

from __future__ import annotations

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
AUTO_ARCHIVED = "cleanup"  # project["archived_by"]: выпуск убрала в архив уборка
STALE_ARCHIVED = "stale"  # project["archived_by"]: брошенный неопубликованный выпуск (archive_stale)


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
        uploads = self.ctx.config.uploads_dir
        for entry in uploads.iterdir() if uploads.is_dir() else []:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                found["uploads"] += 1
                found["bytes"] += entry.stat().st_size
                if not dry:
                    entry.unlink(missing_ok=True)
        studio = self.studio
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

    def archive_stale(self, draft_days: int, *, now: float | None = None) -> list[dict]:
        """Брошенные выпуски — в архив (PRD-004): не опубликованы, не идут и без движения
        (updated_at) дольше draft_days дней. 0 — выключено. Файлы остаются, вернуть можно из архива;
        уборка архива удалит их позже, как любой архивный выпуск. Возвращает убранные (id, title)."""
        studio = self.studio
        if studio is None or not draft_days:
            return []
        now = now or time.time()
        cutoff = now - draft_days * DAY_S
        done = []
        for item in studio.studio.list(archived=False):
            if item["running"] or item["updated_at"] >= cutoff:
                continue
            project = studio.studio.get(item["id"])
            if project is None or project.get("running") or project.get("archived"):
                continue
            if project.get("steps", {}).get("publish", {}).get("status") == "done":
                continue  # опубликованные уходят в архив по archive_days
            studio.studio.archive(item["id"], True)
            studio.studio.annotate(item["id"], archived_by=STALE_ARCHIVED)
            done.append({"id": item["id"], "title": item["title"]})
        if done:
            log.event("cleanup.stale_archived", count=len(done), days=draft_days)
            titles = ", ".join(f"«{p['title']}»" for p in done[:5]) + (" и другие" if len(done) > 5 else "")
            studio.notify(
                f"В архив без движения {draft_days} дн.: {len(done)}",
                f"{titles}. Вернуть — «Выпуски» → «Архив».",
                important=True,
            )
        return done


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
