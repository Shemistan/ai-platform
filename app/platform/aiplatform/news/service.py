"""Вкладка «Новости»: из чего собирается дайджест и обзоры, API под /api/news/.

- Источники: журнал сбора (ок или сбой, когда, сколько нового) и польза —
  сколько важного из разобранного; «Собрать» один источник или все.
- Главное за день: события по важности, вошедшие в выпуск и нет.
- Добор: источники со сбоем; агент добирает их материалы сам (make news-rescue).
- Обзоры для YouTube: неделя и месяц — суфлёр, материалы и пост (агент по
  skill youtube-news, make news-weekly / news-monthly).

Сам выпуск дня делает сценарий «Дайджест новостей».
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from http import HTTPStatus

from aiplatform.core import narrator as narrator_module
from aiplatform.core import tasks as tasks_module
from aiplatform.core.http import HttpError, Request, bad_request, found, not_found
from aiplatform.core.narrator import INSTALL_HINT, NarratorError

PREFIX = "news"
_SOURCE = re.compile(r"^[a-z0-9_]{1,64}$")
_MONTH = re.compile(r"^\d{4}-\d{2}$")
_DONE = re.compile(r"^\s+(ок|сбой|добор)\s+(\S+)")


def progress(line: str) -> str | None:
    if m := _DONE.match(line):
        return f"{m[1]}: {m[2]}"
    lowered = line.lower()
    for word, stage in (("агент", "Агент работает"), ("суфл", "Агент пишет суфлёр"), ("сбор", "Сбор источников")):
        if word in lowered:
            return stage
    return None


def parse_day(value: str | None) -> str:
    """День: пусто — вчера; будущее и мусор — ошибка."""
    if not value:
        return (date.today() - timedelta(days=1)).isoformat()
    try:
        day = date.fromisoformat(value)
    except ValueError as exc:
        raise bad_request("День — дата вида ГГГГ-ММ-ДД.") from exc
    if day > date.today():
        raise bad_request("Этот день ещё не наступил.")
    return day.isoformat()


class NewsModule:
    prefix = PREFIX
    label = "Новости"

    def __init__(self, ctx) -> None:
        self.narrator = narrator_module.load(ctx.config)
        self.tasks = tasks_module.Tasks(ctx.config.data_dir / "news" / "tasks")

    def describe(self) -> dict:
        return {}

    def problems(self) -> list[str]:
        return []

    def _bridge(self, *args: str) -> dict:
        try:
            return self.narrator.bridge(*args)
        except NarratorError as exc:
            message = str(exc)
            status = HTTPStatus.NOT_FOUND if "нет в библиотеке" in message else HTTPStatus.BAD_GATEWAY
            raise HttpError(status, message) from exc

    def handle(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["sources"]):
                data = self._bridge("sources")
                return req.send_json(data | {"tasks": self.tasks.list("collect")[:5]})
            case ("POST", ["sources", "collect"]):
                return req.send_json(self.collect(req.json()), HTTPStatus.ACCEPTED)
            case ("GET", ["failed"]):
                data = self._bridge("failed")
                return req.send_json(data | {"tasks": self.tasks.list("rescue")[:3]})
            case ("POST", ["failed", "rescue"]):
                return req.send_json(self.rescue(req.json()), HTTPStatus.ACCEPTED)
            case ("GET", ["top"]):
                day = parse_day(req.arg("day"))
                issue = self._bridge("issue", "--date", day)
                return req.send_json(self._bridge("top", "--date", day) | {"issue": issue})
            case ("GET", ["reviews"]):
                data = self._bridge("reviews")
                return req.send_json(data | {"tasks": self.tasks.list("review")[:3]})
            case ("POST", ["reviews", kind]) if kind in ("weekly", "monthly"):
                return req.send_json(self.review(kind, req.json()), HTTPStatus.ACCEPTED)
            case ("GET", ["reviews", review_id]):
                return req.send_json(self._bridge("review", review_id))
            case ("GET", ["tasks", task_id]):
                return req.send_json(found(self.tasks.get(task_id)))
            case ("POST", ["tasks", task_id, "cancel"]):
                return req.send_json(found(self.tasks.cancel(task_id)))
        raise not_found()

    def _start(self, kind: str, title: str, argv: list[str], target: str = "") -> dict:
        busy = self.tasks.busy(kind)
        if busy:
            raise HttpError(HTTPStatus.CONFLICT, f"Уже идёт: {busy['title']}.")
        spec = tasks_module.Spec(
            kind=kind,
            title=title,
            argv=argv,
            cwd=self.narrator.cwd,
            env=self.narrator.env,
            target=target,
            hint=INSTALL_HINT,
            progress=progress,
        )
        return self.tasks.start(spec)

    def collect(self, payload: object) -> dict:
        only = payload.get("only") if isinstance(payload, dict) else None
        argv = self.narrator.argv("news", "collect")
        if only:
            if not isinstance(only, str) or not _SOURCE.match(only):
                raise bad_request("Источник — id из конфига источников.")
            argv += ["--only", only]
        return self._start("collect", f"Сбор: {only}" if only else "Сбор всех источников", argv, only or "")

    def rescue(self, payload: object) -> dict:
        day = parse_day(payload.get("day") if isinstance(payload, dict) else None)
        argv = self.narrator.make("news-rescue", DAY=day)
        return self._start("rescue", f"Агент добирает упавшие источники за {day}", argv)

    def review(self, kind: str, payload: object) -> dict:
        if kind == "weekly":
            return self._start("review", "Обзор недели", self.narrator.make("news-weekly"))
        month = payload.get("month") if isinstance(payload, dict) else None
        if month and (not isinstance(month, str) or not _MONTH.match(month)):
            raise bad_request("Месяц — вида ГГГГ-ММ.")
        title = f"Обзор месяца {month}" if month else "Обзор текущего месяца"
        return self._start("review", title, self.narrator.make("news-monthly", MONTH=month or ""))

    def close(self) -> None:
        self.tasks.close()
