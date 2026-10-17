"""GPU-замок: в памяти одна тяжёлая модель за раз.

Фото, видео и озвучка — каждая со своей очередью, но модели на одном
Mac делят одну память. Воркер берёт замок вокруг запуска модели; кто
пришёл раньше, тот и идёт первым (FIFO). Ожидание можно отменить.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from aiplatform.core.runner import Canceled

_POLL_S = 0.2


class Gate:
    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._queue: deque[object] = deque()
        self._holder: str | None = None
        self._since: float | None = None  # time.time() взятия замка — видно, если модель зависла

    def holder(self) -> str | None:
        with self._cond:
            return self._holder

    def held(self) -> dict | None:
        """Кто держит замок, с какого момента и сколько секунд; None — свободен."""
        with self._cond:
            if self._holder is None or self._since is None:
                return None
            return {"owner": self._holder, "since": self._since, "held_s": round(time.time() - self._since, 1)}

    def waiting(self) -> int:
        with self._cond:
            return len(self._queue)

    @contextmanager
    def hold(
        self,
        owner: str,
        on_wait: Callable[[str], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Iterator[None]:
        """Держать замок на время блока. on_wait(кто держит) зовётся один раз,
        если ждать придётся; взведённый cancel во время ожидания → Canceled."""
        ticket = object()
        with self._cond:
            self._queue.append(ticket)
            busy = self._holder is not None or self._queue[0] is not ticket
            current = self._holder
        if busy and on_wait:
            on_wait(current or "очередь")
        with self._cond:
            while self._holder is not None or self._queue[0] is not ticket:
                if cancel is not None and cancel.is_set():
                    self._queue.remove(ticket)
                    self._cond.notify_all()
                    raise Canceled
                self._cond.wait(_POLL_S)
            self._queue.popleft()
            self._holder = owner
            self._since = time.time()
        try:
            yield
        finally:
            with self._cond:
                self._holder = None
                self._since = None
                self._cond.notify_all()
