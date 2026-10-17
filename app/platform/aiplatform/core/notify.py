"""Уведомления человеку: выпуск ждёт его, упал или опубликован.

Каждое уведомление пишется строкой в `<data>/notifications.log`. Если в
настройках уведомления включены, ещё и показывается: командой
AIP_NOTIFY_CMD (заголовок — первая строка stdin, текст — остальное), а без
неё на macOS — системным уведомлением через osascript. В демо — только лог.
"""

from __future__ import annotations

import contextlib
import shlex
import subprocess
import sys
import threading
import time
from pathlib import Path

LOG = "notifications.log"
_OSASCRIPT = (
    "osascript",
    "-e",
    "on run argv",
    "-e",
    'display notification (item 2 of argv) with title "AI Platform" subtitle (item 1 of argv)',
    "-e",
    "end run",
)


class Notifier:
    def __init__(self, data_dir: Path, cmd: str = "", *, fake: bool = False) -> None:
        self.log_path = data_dir / LOG
        self.cmd = shlex.split(cmd) if cmd else []
        self.fake = fake
        self._lock = threading.Lock()

    def send(self, title: str, text: str, *, show: bool) -> None:
        """Записать в лог и, если show, показать. Сбой показа не мешает работе."""
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock, contextlib.suppress(OSError):
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(f"{stamp}\t{title}\t{' '.join(text.split())}\n")
        if not show or self.fake:
            return
        if self.cmd:
            argv, stdin = self.cmd, f"{title}\n{text}".encode()
        elif sys.platform == "darwin":
            argv, stdin = [*_OSASCRIPT, title, text], None
        else:
            return
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(argv, input=stdin, capture_output=True, timeout=15, check=False)

    def recent(self, limit: int = 20) -> list[dict]:
        try:
            lines = self.log_path.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        out = []
        for line in reversed(lines):
            at, _, rest = line.partition("\t")
            title, _, text = rest.partition("\t")
            out.append({"at": at, "title": title, "text": text})
        return out
