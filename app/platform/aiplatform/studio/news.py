"""Сбор новостей и пересказ — через narrator: `make news-collect news-agent news-judge DAY=…`.

narrator обходит источники, агент пишет пересказ (narration.md) и пост
(post.md), судья проверяет выпуск. Где лежит выпуск, говорит
`narrate news where`: строка «Озвучить: <каталог>/narration.md → …».
"""

from __future__ import annotations

import re
import threading
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path

from aiplatform.core import runner
from aiplatform.studio.config import StudioConfig
from aiplatform.studio.llm import StudioError

NARRATION = "narration.md"
POST = "post.md"
_WHERE = re.compile(r"Озвучить:\s+(.+?" + re.escape(NARRATION) + r")")
INSTALL_HINT = "Нужен narrator (07_offline_text-to-word) и claude CLI; путь — AIP_NARRATOR_DIR."


def parse_day(value: object, today: date | None = None) -> date:
    """День выпуска: пусто — вчера; будущее — ошибка."""
    today = today or date.today()
    if value in (None, ""):
        return today - timedelta(days=1)
    try:
        day = date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        day = None
    if day is None:
        raise ValueError("День выпуска — дата вида ГГГГ-ММ-ДД.")
    if day > today:
        raise ValueError("Этот день ещё не наступил: дайджест собирают за прошедший.")
    return day


def parse_where(lines: list[str], day: str) -> Path:
    for line in lines:
        if m := _WHERE.search(line):
            return Path(m[1].strip()).parent
    if any("нет" in line and "Дайджеста" in line for line in lines):
        raise StudioError(f"Дайджеста за {day} нет: narrator не собрал выпуск (см. лог шага).")
    raise StudioError("narrator не сказал, где лежит выпуск: нет строки «Озвучить: …/narration.md».")


def collect(
    day: str,
    config: StudioConfig,
    cancel: threading.Event,
    on_line: Callable[[str], None],
    *,
    skip_collect: bool = False,
) -> Path:
    """Собрать выпуск дня (если нужно) и вернуть его каталог с narration.md."""
    cwd = config.narrator_dir if config.narrator_dir.is_dir() else None
    if not skip_collect:
        try:
            runner.run(config.news_argv(config.news_cmd, day), on_line, cancel, cwd=cwd, missing_hint=INSTALL_HINT)
        except runner.RunFailed as exc:
            raise StudioError(f"Сбор новостей: {exc}") from exc
    lines: list[str] = []

    def keep(line: str) -> None:
        lines.append(line)
        on_line(line)

    try:
        runner.run(config.news_argv(config.news_where, day), keep, cancel, cwd=cwd, missing_hint=INSTALL_HINT)
    except runner.RunFailed as exc:
        raise StudioError(f"narrate news where: {exc}") from exc
    folder = parse_where(lines, day)
    if not (folder / NARRATION).is_file():
        raise StudioError(f"В {folder} нет {NARRATION}.")
    return folder
