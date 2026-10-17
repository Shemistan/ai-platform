"""Свободное место на диске данных: проверка до тяжёлых задач.

Диск кончается посреди генерации или сборки — ffmpeg пишет обрезанный файл,
в Telegram уходит битое видео. Поэтому GPU-задачи (через GPU-замок) и сборка
видео сначала зовут `ensure_free_space`: мало места — задача не стартует,
ошибка понятна («Свободно X ГБ, нужно Y»), владельцу — уведомление (не чаще
раза в час).

Порог — `cleanup.min_free_gb` в settings.json; не задан — 10 ГБ, в демо и
тестах (AIP_BACKEND=fake) — без проверки. 0 — проверка выключена.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import log
from aiplatform.core.runner import RunFailed

DEFAULT_MIN_FREE_GB = 10.0
NOTIFY_EVERY_S = 3600  # одно уведомление о нехватке места в час, а не на каждую задачу
HINT = "Освободи место (Настройки → Уборка) или поменяй порог cleanup.min_free_gb в settings.json."


class NoSpace(RunFailed):
    """Мало места на диске данных: задача не стартовала."""


def free_gb(path: Path) -> float:
    """Свободно на диске, где лежит path (или его ближайший существующий родитель), ГБ."""
    probe = Path(path)
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    return shutil.disk_usage(probe).free / 2**30


def ensure_free_space(path: Path, need_gb: float, what: str = "") -> float:
    """Свободно не меньше need_gb — вернуть сколько; иначе NoSpace с понятным текстом."""
    free = free_gb(path)
    if need_gb > 0 and free < need_gb:
        head = f"{what}: " if what else ""
        raise NoSpace(f"{head}свободно {free:.1f} ГБ на диске данных, нужно от {need_gb:g} ГБ. {HINT}")
    return free


def threshold(settings: dict, *, fake: bool) -> float:
    """Порог из настроек: число — как есть, не задан — по умолчанию (в демо — без проверки)."""
    value = settings.get("cleanup", {}).get("min_free_gb")
    if value is None:
        return 0.0 if fake else DEFAULT_MIN_FREE_GB
    return float(value)


class Guard:
    """Проверка места перед тяжёлой задачей: guard("генерация картинки")."""

    def __init__(
        self,
        path: Path,
        need: Callable[[], float],
        notify: Callable[[str, str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.need = need
        self.notify = notify
        self.clock = clock
        self._lock = threading.Lock()
        self._notified_at: float | None = None

    def __call__(self, what: str = "") -> None:
        try:
            need = self.need()
        except Exception:  # noqa: BLE001 — битые настройки не мешают запуску
            need = DEFAULT_MIN_FREE_GB
        if need <= 0:
            return
        try:
            ensure_free_space(self.path, need, what)
        except NoSpace as exc:
            log.event("disk.low", level=logging.WARNING, what=what, error=str(exc))
            self._alert(str(exc))
            raise
        except OSError:  # каталог данных недоступен — это скажет сама задача
            return

    def _alert(self, text: str) -> None:
        if self.notify is None:
            return
        with self._lock:
            now = self.clock()
            if self._notified_at is not None and now - self._notified_at < NOTIFY_EVERY_S:
                return
            self._notified_at = now
        try:
            self.notify("Мало места на диске", text)
        except Exception:  # noqa: BLE001 — сбой уведомления не меняет ошибку задачи
            log.event("notify.failed", level=logging.WARNING, via="disk")


def guard_for(config, notifier) -> Guard:
    """Guard платформы: порог — из settings.json, уведомление — если включены в настройках."""

    def need() -> float:
        return threshold(config.settings.load(), fake=config.fake)

    def notify(title: str, text: str) -> None:
        notifier.send(title, text, show=config.settings.load()["notify"]["enabled"])

    return Guard(config.data_dir, need, notify)
