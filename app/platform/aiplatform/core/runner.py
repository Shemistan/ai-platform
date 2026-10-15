"""Запуск модели или инструмента подпроцессом: вывод построчно, отмена всей группы."""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

_SPLIT = re.compile(rb"[\r\n]+")
_TERMINATE_GRACE_S = 5
DEFAULT_MISSING_HINT = "Поставь пайплайн по README платформы, раздел «Установка моделей»."


class Canceled(Exception):
    pass


class RunFailed(Exception):
    def __init__(self, message: str, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode


def run(
    argv: list[str],
    on_line: Callable[[str], None],
    cancel: threading.Event,
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    stdin: bytes | None = None,
    missing_hint: str = DEFAULT_MISSING_HINT,
) -> None:
    """Выполнить команду. Каждая строка вывода (разделители `\\n` и `\\r`,
    чтобы видеть обновления tqdm) уходит в on_line. Код возврата ≠ 0 →
    RunFailed, взведённый cancel → Canceled. stdin — байты на вход процессу."""
    if cancel.is_set():
        raise Canceled
    full_env = {**os.environ, **(env or {}), "PYTHONUNBUFFERED": "1"}
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
            cwd=cwd,
            env=full_env,
            start_new_session=True,  # своя группа: отмена гасит и дочерние процессы
        )
    except FileNotFoundError as exc:
        raise RunFailed(f"Команда «{argv[0]}» не найдена. {missing_hint}") from exc

    stop_watch = threading.Event()

    def watch_cancel() -> None:
        while not stop_watch.wait(0.2):
            if cancel.is_set():
                _kill_group(proc)
                return

    def feed() -> None:
        assert proc.stdin is not None
        with contextlib.suppress(OSError):  # процесс мог не читать stdin и уже выйти
            proc.stdin.write(stdin or b"")
        with contextlib.suppress(OSError):
            proc.stdin.close()

    watcher = threading.Thread(target=watch_cancel, daemon=True)
    watcher.start()
    feeder = threading.Thread(target=feed, daemon=True) if stdin is not None else None
    if feeder:
        feeder.start()
    try:
        assert proc.stdout is not None
        fd = proc.stdout.fileno()
        pending = b""
        while chunk := os.read(fd, 65536):
            *lines, pending = _SPLIT.split(pending + chunk)
            for raw in lines:
                on_line(raw.decode("utf-8", "replace"))
        if pending:
            on_line(pending.decode("utf-8", "replace"))
        returncode = proc.wait()
    finally:
        stop_watch.set()
        if proc.poll() is None:
            _kill_group(proc)
        if proc.stdout is not None:
            proc.stdout.close()
        watcher.join()
        if feeder:
            feeder.join(timeout=5)

    if cancel.is_set():
        raise Canceled
    if returncode != 0:
        raise RunFailed(_describe_exit(returncode), returncode)


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return
        try:
            proc.wait(timeout=_TERMINATE_GRACE_S)
            return
        except subprocess.TimeoutExpired:
            continue


def _describe_exit(returncode: int) -> str:
    if returncode == -signal.SIGKILL:
        return (
            "Процесс убит системой — скорее всего, не хватило памяти. "
            "Уменьши размер, выбери модель легче или включи режим экономии памяти (LOW_RAM)."
        )
    if returncode < 0:
        return f"Процесс завершён сигналом {-returncode}."
    return f"Процесс завершился с кодом {returncode}."
