"""Одна модель за раз: озвучка, картинки и клипы ждут друг друга по очереди.

Мак не выдерживает две модели в памяти сразу. ModelGate — замок с очередью в
порядке прихода: кто раньше попросил, тот раньше и получит, поэтому подготовка
видео не ждёт, пока озвучатся все десять статей очереди, а встаёт между ними.
Когда замок переходит к другому владельцу, его прежний владелец выгружает
свою модель — на это регистрируется on_leave (у озвучки — закрыть синтезатор).
"""

from __future__ import annotations

import contextlib
import threading
from collections import deque
from collections.abc import Callable, Iterator


class ModelGate:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._queue: deque[object] = deque()
        self._busy = False
        self._last: str | None = None
        self._holder: str | None = None
        self._on_leave: dict[str, Callable[[], None]] = {}

    def on_leave(self, owner: str, unload: Callable[[], None]) -> None:
        """unload зовётся, когда замок уходит от owner к другому: модель owner не нужна."""
        self._on_leave[owner] = unload

    @property
    def holder(self) -> str | None:
        """Кто сейчас держит модель в памяти: «озвучка», «картинки»…; None — никто."""
        return self._holder

    @contextlib.contextmanager
    def hold(self, owner: str, cancel: threading.Event | None = None) -> Iterator[bool]:
        """Ждёт своей очереди; отдаёт True. Отменили, пока ждали, — False, замок не взят."""
        ticket = object()
        granted = True
        with self._cond:
            self._queue.append(ticket)
            while self._busy or self._queue[0] is not ticket:
                if cancel is not None and cancel.is_set():
                    self._queue.remove(ticket)
                    self._cond.notify_all()
                    granted = False
                    break
                self._cond.wait(0.2)
            if granted:
                self._queue.popleft()
                self._busy = True
            previous = self._last
        if not granted:
            yield False
            return
        if previous and previous != owner and previous in self._on_leave:
            with contextlib.suppress(Exception):  # выгрузка не должна ронять чужую задачу
                self._on_leave[previous]()
        self._holder = self._last = owner
        try:
            yield True
        finally:
            with self._cond:
                self._busy = False
                self._holder = None
                self._cond.notify_all()

    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)
