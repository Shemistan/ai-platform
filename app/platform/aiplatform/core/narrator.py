"""Движок narrator (app/narrator): его CLI для долгих задач и мост `narrate bridge` для данных.

Мост печатает один JSON-объект; ошибку для человека — полем error с кодом 1,
её тип — полем kind (unavailable / not_found / invalid): по нему выбирается HTTP-код.
В демо и тестах вместо движка — fakes/fake_narrate.py: те же команды и ответы,
данные — в каталоге FAKE_NARRATOR_HOME внутри данных платформы.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path

from aiplatform.config import Config, pick
from aiplatform.core.http import HttpError

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "narrator"  # app/narrator рядом с app/platform
FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_narrate.py"
FAKE_HOME_ENV = "FAKE_NARRATOR_HOME"
BRIDGE_TIMEOUT_S = 120  # первый запуск uv собирает окружение — дольше обычного
REGISTRY_ENV = "NARRATOR_REGISTRY"  # путь к registry.json для пометок в подписях narrator (ADR 0001)
REGISTRY_FILE = "registry.json"  # = aiplatform.updates.registry.FILE_NAME, в корне data
INSTALL_HINT = "Нужен движок app/narrator: `make models` в нём (или задай AIP_NARRATE_CMD)."
# Версия контракта моста, с которой работает платформа (narrator.bridge.BRIDGE_API).
EXPECTED_BRIDGE_API = 1
# Команды моста, которые только читают: при падении процесса на запуске их повторяем один раз (BE-013).
# Таймаут не повторяем — второй заход удвоил бы ожидание; запросы с записью — тоже (могли сработать).
READS = frozenset(
    {
        "version",
        "engines",
        "library",
        "article",
        "bot",
        "voices",
        "models",
        "sources",
        "failed",
        "issue",
        "top",
        "reviews",
        "preview",
        "split",
    }
)
RETRY_PAUSE_S = 0.5
CHECK_TTL_S = 60.0  # check() дёргают problems() всех вкладок и каждый /api/config
_MISSING_CMD = re.compile(r"invalid choice: '?(bridge|version)\b")
_checks: dict[tuple, tuple[float, str | None]] = {}
_checks_lock = threading.Lock()
_sleep = time.sleep  # тесты подменяют


def reset_checks() -> None:
    """Забыть результаты check() — для тестов."""
    with _checks_lock:
        _checks.clear()


UNAVAILABLE = "unavailable"  # нет команды, таймаут, падение процесса — вина зависимости
NOT_FOUND = "not_found"  # нет статьи / обзора
INVALID = "invalid"  # движок отверг запрос
KIND_STATUS = {
    UNAVAILABLE: HTTPStatus.SERVICE_UNAVAILABLE,
    NOT_FOUND: HTTPStatus.NOT_FOUND,
    INVALID: HTTPStatus.BAD_REQUEST,
}

KIND_CODE = {UNAVAILABLE: "engine_unavailable", NOT_FOUND: "not_found", INVALID: "engine_rejected"}


class NarratorError(RuntimeError):
    """Движок ответил ошибкой или не запустился; текст — для человека, kind — для HTTP-кода."""

    def __init__(self, message: str, kind: str = UNAVAILABLE, crashed: bool = False) -> None:
        super().__init__(message)
        self.kind = kind if kind in KIND_STATUS else INVALID
        self.crashed = crashed  # процесс упал без ответа — для чтения стоит один повтор


def http_error(exc: NarratorError) -> HttpError:
    """Единый маппинг ошибки движка в ответ ручки: unavailable → 503, not_found → 404, invalid → 400."""
    return HttpError(KIND_STATUS[exc.kind], str(exc), {"kind": exc.kind}, code=KIND_CODE[exc.kind])


@dataclass(frozen=True)
class Narrator:
    cmd: tuple[str, ...]
    home: Path
    env: dict[str, str] = field(default_factory=dict)
    fake: bool = False

    def argv(self, *args: str) -> list[str]:
        return [*self.cmd, *args]

    def make(self, target: str, **params: str) -> list[str]:
        """Цель Makefile движка: агенты (claude -p) и скачивание моделей. В демо — фейк."""
        pairs = [f"{k}={v}" for k, v in params.items() if v not in (None, "")]
        if self.fake:
            return [*self.cmd, "make", target, *pairs]
        return ["make", "-C", str(self.home), target, *pairs]

    @property
    def cwd(self) -> Path | None:
        return self.home if self.home.is_dir() else None

    def available(self) -> bool:
        return self.fake or shutil.which(self.cmd[0]) is not None

    def bridge(self, *args: str, stdin: str | dict | None = None, timeout: float = BRIDGE_TIMEOUT_S) -> dict:
        """`narrate bridge <args>` → ответ JSON. NarratorError — ошибка движка или запуска.

        Чтение (READS), чей процесс упал без ответа, повторяем один раз: разовый сбой
        запуска (uv собирает окружение, гонка за файл) не должен ронять вкладку.
        """
        try:
            return self._bridge_once(*args, stdin=stdin, timeout=timeout)
        except NarratorError as exc:
            if not exc.crashed or not args or args[0] not in READS:
                raise
        _sleep(RETRY_PAUSE_S)
        return self._bridge_once(*args, stdin=stdin, timeout=timeout)

    def _bridge_once(self, *args: str, stdin: str | dict | None, timeout: float) -> dict:
        data = json.dumps(stdin, ensure_ascii=False) if isinstance(stdin, dict) else stdin
        try:
            done = subprocess.run(
                self.argv("bridge", *args),
                input=data or "",
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=self.cwd,
                env={**os.environ, **self.env},
                check=False,
            )
        except FileNotFoundError as exc:
            raise NarratorError(f"Команда «{self.cmd[0]}» не найдена. {INSTALL_HINT}") from exc
        except subprocess.TimeoutExpired as exc:
            raise NarratorError(f"Движок не ответил за {timeout:g} с: bridge {' '.join(args)}.") from exc
        try:
            payload = json.loads(done.stdout.strip().splitlines()[-1]) if done.stdout.strip() else None
        except (json.JSONDecodeError, IndexError):
            payload = None
        if isinstance(payload, dict) and payload.get("error"):
            # Движок до kind в ответе: ошибка с текстом — отказ в запросе, не авария.
            raise NarratorError(str(payload["error"]), str(payload.get("kind") or INVALID))
        if done.returncode != 0 or not isinstance(payload, dict):
            tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
            text = " ".join(tail)
            # «Нет такой команды» у старого движка — не сбой запуска, повтор не поможет.
            crashed = not _MISSING_CMD.search(text)
            raise NarratorError("Движок упал: " + (text or f"код {done.returncode}"), crashed=crashed)
        return payload

    def check(self) -> str | None:
        """Совместим ли движок с платформой: None или текст проблемы для баннера.

        Один вызов `bridge version` на движок раз в CHECK_TTL_S: результат общий
        для всех вкладок (у каждой свой экземпляр Narrator с теми же полями).
        """
        key = (self.cmd, str(self.home), tuple(sorted(self.env.items())), self.fake)
        now = time.monotonic()
        with _checks_lock:
            cached = _checks.get(key)
        if cached and now - cached[0] < CHECK_TTL_S:
            return cached[1]
        problem = self._check()
        with _checks_lock:
            _checks[key] = (now, problem)
        return problem

    def _check(self) -> str | None:
        where = f"Движок narrator по пути {self.home}"
        fix = (
            f"Укажи AIP_NARRATOR_DIR={DEFAULT_DIR} в app/platform/local.mk "
            "или убери строку — по умолчанию берётся app/narrator."
        )
        if not self.available():
            return f"Команда «{self.cmd[0]}» не найдена. {INSTALL_HINT}"
        try:
            got = self.bridge("version").get("bridge_api")
        except NarratorError as exc:
            missing = _MISSING_CMD.search(str(exc))
            if missing and missing.group(1) == "bridge":
                return f"{where} устарел: нет команды bridge. {fix}"
            if missing:
                return f"{where} устарел: нет bridge version, нужна версия {EXPECTED_BRIDGE_API}. {fix}"
            return f"{where} не отвечает: {exc}"
        if got != EXPECTED_BRIDGE_API:
            return f"{where} несовместим: версия моста {got}, нужна {EXPECTED_BRIDGE_API}. {fix}"
        return None


def load(config: Config) -> Narrator:
    home = Path(pick(config.env, "AIP_NARRATOR_DIR", default=str(DEFAULT_DIR))).expanduser()
    if config.fake:
        fake_home = config.data_dir / "fake-narrator"
        return Narrator((sys.executable, str(FAKE)), home, {FAKE_HOME_ENV: str(fake_home)}, fake=True)
    cmd = tuple(shlex.split(pick(config.env, "AIP_NARRATE_CMD", default=str(home / "narrate"))))
    # Реестр платформы (вкладка «Обновления») — и для пометок в подписях narrator.
    own = pick(config.env, REGISTRY_ENV, default=str(config.data_dir / REGISTRY_FILE))
    return Narrator(cmd, home, {REGISTRY_ENV: own})
