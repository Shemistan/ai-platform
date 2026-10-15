"""Общее состояние прогресса; разбор вывода конкретного пайплайна — в mflux.py и flux2.py."""

from __future__ import annotations

# Доли общего прогресса: подготовка, шумоподавление, декодирование и запись.
LOADING = 5.0
DENOISE_FROM, DENOISE_TO = 12.0, 92.0
DECODING = 95.0
FINISHING_CAP = 99.0


class Progress:
    """Копит состояние по строкам вывода процесса. Процент только растёт."""

    def __init__(self) -> None:
        self.stage = "Запуск"
        self.step: int | None = None
        self.steps: int | None = None
        self.percent = 0.0

    def snapshot(self) -> dict:
        return {"stage": self.stage, "step": self.step, "steps": self.steps, "percent": round(self.percent, 1)}

    def set_stage(self, stage: str, percent: float | None = None) -> None:
        self.stage = stage
        self.step = self.steps = None
        if percent is not None:
            self.raise_percent(percent)

    def denoise(self, n: int, total: int) -> None:
        self.stage = "Генерация"
        self.step, self.steps = n, total
        self.raise_percent(DENOISE_FROM + (DENOISE_TO - DENOISE_FROM) * (n / total if total else 0.0))
        if total and n >= total:
            self.set_stage("Декодирование и сохранение", DECODING)

    def raise_percent(self, value: float) -> None:
        self.percent = max(self.percent, min(value, FINISHING_CAP))

    def feed(self, line: str) -> bool:
        """Учесть строку. True — если это строка прогресса (в лог её не пишем)."""
        raise NotImplementedError
