"""Этапы работы монтажа (PRD-025): что входит в действие, где оно сейчас и сколько осталось.

Проект хранит список этапов текущего действия — `project["stages"]`: id, подпись, вес, статус
(pending → running → done; failed / canceled; skipped — до него не дошло, а действие кончилось),
процент, деталь и время начала/конца. Общий процент — по весам; «осталось» — текущий этап по его
скорости, впереди — по весам, пересчитанным на скорость уже сделанного.
"""

from __future__ import annotations

import time

# id → (подпись, вес). Вес — грубая доля времени этапа в действии; точность даёт пересчёт по скорости.
STAGES = {
    "audio": ("Звук из записи", 3),
    "sync": ("Сдвиг экрана по звуку", 2),
    "silences": ("Паузы", 2),
    "transcribe": ("Расшифровка", 40),
    "plan": ("План монтажа (ИИ)", 8),
    "segments": ("Сборка кусков", 25),
    "join": ("Склейка", 2),
    "final": ("Громкость и субтитры", 8),
    "check": ("Проверка результата", 20),
    "motion": ("Моушн-вставки", 12),
}
RENDER = ("segments", "join", "final")
MIN_PERCENT_FOR_ETA = 2.0  # раньше скорость этапа не видна — оценка врала бы


def plan_for(action: str, options: dict, *, screen_audio: bool = False) -> list[str]:
    """Этапы действия по порядку: prepare (и сборка, если auto), replan, revise, render."""
    render = [*RENDER, *(["check"] if options.get("check") else []), *(["motion"] if options.get("motion") else [])]
    auto = options.get("mode") == "autonomous" or options.get("auto")
    if action == "prepare":
        head = ["audio", *(["sync"] if screen_audio else []), "silences", "transcribe"]
        if options.get("mode") != "manual":
            head.append("plan")
        return [*head, *(render if auto else [])]
    if action in ("replan", "revise"):
        return ["plan"]
    return render


def new(ids: list[str]) -> list[dict]:
    return [
        {"id": i, "label": STAGES[i][0], "weight": STAGES[i][1], "status": "pending", "percent": 0.0, "detail": "",
         "started_at": None, "finished_at": None}
        for i in ids
    ]  # fmt: skip


def advance(stages: list[dict], stage_id: str, percent: float, detail: str = "", now: float | None = None) -> None:
    """Этап идёт с таким процентом; этапы до него — сделаны. Неизвестный этап — без изменений."""
    now = time.time() if now is None else now
    index = next((n for n, s in enumerate(stages) if s["id"] == stage_id), None)
    if index is None:
        return
    for earlier in stages[:index]:
        if earlier["status"] in ("pending", "running"):
            _close(earlier, "done", now)
    stage = stages[index]
    if stage["status"] != "running":
        stage.update(status="running", started_at=now, finished_at=None, percent=0.0)
    stage["percent"] = round(max(stage["percent"], min(max(percent, 0.0), 100.0)), 1)
    stage["detail"] = detail


def finish(stages: list[dict], status: str, now: float | None = None) -> None:
    """Действие кончилось: done/review — идущие сделаны, недошедшие пропущены; failed/canceled — идущий
    этап получает этот статус, остальные остаются впереди."""
    now = time.time() if now is None else now
    for stage in stages:
        if stage["status"] == "running":
            _close(stage, "done" if status in ("done", "review") else status, now)
        elif stage["status"] == "pending" and status in ("done", "review"):
            stage["status"] = "skipped"


def _close(stage: dict, status: str, now: float) -> None:
    stage["status"] = status
    stage["finished_at"] = now
    stage["started_at"] = stage["started_at"] or now
    if status == "done":
        stage["percent"] = 100.0
        stage["detail"] = ""


def current(stages: list[dict]) -> dict | None:
    return next((s for s in stages if s["status"] == "running"), None)


def overall(stages: list[dict]) -> float:
    """Общий процент по весам: сделанные и пропущенные — целиком, идущий — своей долей."""
    total = sum(s["weight"] for s in stages) or 1
    done = sum(s["weight"] * (1.0 if s["status"] in ("done", "skipped") else s["percent"] / 100) for s in stages)
    return round(100 * done / total, 1)


def remaining(stages: list[dict], now: float | None = None) -> float | None:
    """Секунд до конца действия, или None — пока не по чему судить.

    Текущий этап — по своей скорости; впереди — вес × секунды на единицу веса сделанного (с долей
    текущего). Ничего не сделано и текущий в самом начале — None.
    """
    now = time.time() if now is None else now
    stage = current(stages)
    if stage is None:
        return None
    spent = weight_done = 0.0
    for s in stages:
        if s["status"] == "done" and s["started_at"] and s["finished_at"]:
            spent += s["finished_at"] - s["started_at"]
            weight_done += s["weight"]
    running = now - (stage["started_at"] or now)
    known = stage["percent"] >= MIN_PERCENT_FOR_ETA
    if known:
        spent += running
        weight_done += stage["weight"] * stage["percent"] / 100
    if weight_done <= 0 or spent <= 0:
        return None
    per_weight = spent / weight_done
    if known:
        left_here = running * (100 - stage["percent"]) / stage["percent"]
    else:
        left_here = max(stage["weight"] * per_weight - running, 0.0)
    ahead = sum(s["weight"] for s in stages if s["status"] == "pending")
    return round(left_here + ahead * per_weight)


def stage_remaining(stage: dict, now: float | None = None) -> float | None:
    """Секунд до конца идущего этапа по его скорости; None — рано судить."""
    now = time.time() if now is None else now
    if stage["status"] != "running" or stage["percent"] < MIN_PERCENT_FOR_ETA or not stage["started_at"]:
        return None
    running = now - stage["started_at"]
    return round(running * (100 - stage["percent"]) / stage["percent"])
