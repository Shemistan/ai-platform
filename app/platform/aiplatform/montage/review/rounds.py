"""Стадии раунда ревью (ITERATIVE_REVIEW_SPEC §9): каркас прогона, который воркер зовёт действием `review`.

`drive` читает запуск с чекпоинта и ведёт его по состояниям, пока не упрётся в ожидание человека,
паузу, конец раунда или конец запуска. Стадии добавляются подзадачами плана: без них запуск честно
встаёт в `blocked` с причиной — никаких поддельных результатов.
"""

from __future__ import annotations

import threading

from aiplatform.core import runner
from aiplatform.montage.review import state

NOT_READY = "Стадии раунда ещё не реализованы в этой сборке платформы."


def drive(review, project: dict, run_id: str, cancel: threading.Event) -> str:
    folder = review.folder(project["id"])
    run = state.load_run(folder, run_id)
    if run is None:
        raise runner.RunFailed("Запуск ревью пропал из каталога проекта.")
    if run["state"] == "paused":  # человек нажал паузу, пока действие стояло в очереди
        return "Ревью на паузе"
    if not review.check_lock(run):
        return "Ревью устарело"
    if run["state"] == "preparing":
        run["blocking_reason"] = NOT_READY
        state.advance(run, "blocked", reason="not_implemented")
        state.checkpoint(folder, run)
    return "Ревью заблокировано" if run["state"] == "blocked" else "Готово"
