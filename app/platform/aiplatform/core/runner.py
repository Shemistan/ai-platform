"""Запуск модели или инструмента подпроцессом: вывод построчно, отмена всей группы.

Каждый процесс — в своей группе (отмена гасит и дочерние). Пока он жив, в
`<data>/run/<pgid>.json` лежит его pid-файл: после аварийного конца
платформы новый процесс находит сирот по этим файлам и гасит их
(`reap_orphans`), чтобы две тяжёлые модели не делили память. Остановка
сервиса гасит все живые группы сразу (`kill_all`).

Лимиты времени: `timeout_s` — на весь запуск, `idle_timeout_s` — «нет вывода
N секунд». Не заданы явно — берутся по виду (`kind`: llm, image, render,
video, audio, news) из настроек (`timeouts` в settings.json, минуты) или
дефолтов LIMITS. Превышение — RunFailed «Превышено N мин: <что>».
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import log

_SPLIT = re.compile(rb"[\r\n]+")
_TERMINATE_GRACE_S = 5
DEFAULT_MISSING_HINT = "Поставь пайплайн по README платформы, раздел «Установка моделей»."
# Дефолтные лимиты, минуты: (весь запуск, без вывода). None — без лимита.
LIMITS: dict[str, tuple[float | None, float | None]] = {
    "llm": (10, None),
    "image": (15, None),
    "render": (30, None),
    "video": (60, None),
    "audio": (90, None),
    "news": (30, None),
}
KIND_LABELS = {
    "llm": "LLM",
    "image": "генерация картинки",
    "render": "сборка видео",
    "video": "генерация видео",
    "audio": "озвучка",
    "news": "сбор новостей",
}

_run_dir: Path | None = None
_limits: Callable[[], dict] | None = None
_live: dict[int, subprocess.Popen] = {}
_live_lock = threading.Lock()
_stopping = threading.Event()  # сервис останавливается: процессы гасит kill_all, это не сбой
_stopped_pids: set[int] = set()  # погашенные kill_all: их run кончается Canceled


def configure(run_dir: Path | None, limits: Callable[[], dict] | None = None) -> None:
    """Куда класть pid-файлы и откуда брать переопределения лимитов (App при старте)."""
    global _run_dir, _limits
    _run_dir, _limits = run_dir, limits
    _stopping.clear()


def limit(kind: str | None) -> tuple[float | None, float | None]:
    """(timeout_s, idle_timeout_s) для вида процесса: настройки поверх дефолтов."""
    if kind is None:
        return None, None
    total, idle = LIMITS.get(kind, (None, None))
    try:
        custom = (_limits() if _limits else {}).get(kind) or {}
    except Exception:  # noqa: BLE001 — битые настройки не мешают запуску
        custom = {}
    total = custom.get("total", total)
    idle = custom.get("idle", idle)
    return (total * 60 if total else None), (idle * 60 if idle else None)


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
    kind: str | None = None,
    timeout_s: float | None = None,
    idle_timeout_s: float | None = None,
) -> None:
    """Выполнить команду. Каждая строка вывода (разделители `\\n` и `\\r`,
    чтобы видеть обновления tqdm) уходит в on_line. Код возврата ≠ 0 →
    RunFailed, взведённый cancel → Canceled. stdin — байты на вход процессу.
    kind — вид процесса для лимитов по умолчанию; timeout_s / idle_timeout_s
    задают их явно. Остановка сервиса (kill_all) — тоже Canceled: шаг не сломан."""
    if cancel.is_set() or _stopping.is_set():
        raise Canceled
    default_total, default_idle = limit(kind)
    timeout_s = timeout_s if timeout_s is not None else default_total
    idle_timeout_s = idle_timeout_s if idle_timeout_s is not None else default_idle
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
    _register(proc, argv)

    stop_watch = threading.Event()
    started = last_output = time.monotonic()
    expired: list[str] = []  # чем кончилось терпение: текст ошибки

    def watch_cancel() -> None:
        while not stop_watch.wait(0.2):
            if cancel.is_set():
                _kill_group(proc)
                return
            now = time.monotonic()
            if timeout_s and now - started > timeout_s:
                expired.append(f"Превышено {_minutes(timeout_s)}: {_what(kind, argv)}.")
            elif idle_timeout_s and now - last_output > idle_timeout_s:
                expired.append(f"Нет вывода {_minutes(idle_timeout_s)}: {_what(kind, argv)} завис.")
            if expired:
                log.event("process.timeout", level=logging.WARNING, argv0=argv[0], kind=kind, error=expired[0])
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
            last_output = time.monotonic()
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
        _unregister(proc)

    with _live_lock:
        stopped = proc.pid in _stopped_pids
        _stopped_pids.discard(proc.pid)
    if cancel.is_set() or _stopping.is_set() or stopped:
        raise Canceled
    if expired:
        raise RunFailed(expired[0], returncode)
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


def _minutes(seconds: float) -> str:
    return f"{seconds / 60:g} мин" if seconds >= 60 else f"{seconds:g} с"


def _what(kind: str | None, argv: list[str]) -> str:
    return KIND_LABELS.get(kind or "", Path(argv[0]).name)


# --- живые группы и pid-файлы ---


def _pid_file(pgid: int) -> Path | None:
    return _run_dir / f"{pgid}.json" if _run_dir is not None else None


def _register(proc: subprocess.Popen, argv: list[str]) -> None:
    with _live_lock:
        _live[proc.pid] = proc
        late = _stopping.is_set()
    if late:  # стартовал, пока сервис уже гасил процессы
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
    path = _pid_file(proc.pid)
    if path is None:
        return
    with contextlib.suppress(OSError):  # каталог данных мог исчезнуть (тесты) — запуск важнее
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({"pgid": proc.pid, "argv": argv, "started_at": time.time()}), encoding="utf-8")


def _unregister(proc: subprocess.Popen) -> None:
    with _live_lock:
        _live.pop(proc.pid, None)
    path = _pid_file(proc.pid)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def stopping() -> bool:
    """Сервис останавливается (был kill_all): сбои процессов сейчас — не ошибки шагов."""
    return _stopping.is_set()


def kill_all() -> int:
    """Остановка сервиса: погасить все живые группы сразу (TERM всем, потом KILL). Вернуть, сколько."""
    with _live_lock:
        _stopping.set()
        procs = list(_live.values())
        # Флаг _stopping сбросит configure(None) при закрытии App — поток run мог ещё не дойти
        # до проверки. Метка на самом процессе не пропадёт: его гашение — отмена, а не сбой.
        _stopped_pids.update(proc.pid for proc in procs)
    for proc in procs:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
    deadline = time.monotonic() + _TERMINATE_GRACE_S
    for proc in procs:
        try:
            proc.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
    return len(procs)


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _command_of(pid: int) -> str:
    try:
        done = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.strip()


def reap_orphans(run_dir: Path) -> list[dict]:
    """Старт платформы: погасить группы прошлого процесса, которые пережили его. Вернуть погашенные."""
    killed = []
    for path in sorted(run_dir.glob("*.json")) if run_dir.is_dir() else []:
        try:
            info = json.loads(path.read_text(encoding="utf-8"))
            pgid, argv = int(info["pgid"]), [str(a) for a in info["argv"]]
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        # pid мог достаться чужому процессу: гасим, только если команда — наша.
        if pgid > 1 and argv and _group_alive(pgid) and argv[0] in _command_of(pgid):
            for sig in (signal.SIGTERM, signal.SIGKILL):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(pgid, sig)
                for _ in range(int(_TERMINATE_GRACE_S / 0.1)):
                    if not _group_alive(pgid):
                        break
                    time.sleep(0.1)
                if not _group_alive(pgid):
                    break
            killed.append({"pgid": pgid, "argv": argv})
            log.event("process.orphan_killed", level=logging.WARNING, pgid=pgid, argv0=argv[0])
        path.unlink(missing_ok=True)
    return killed
