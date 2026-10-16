"""Движок narrator (app/narrator): его CLI для долгих задач и мост `narrate bridge` для данных.

Мост печатает один JSON-объект; ошибку для человека — полем error с кодом 1.
В демо и тестах вместо движка — fakes/fake_narrate.py: те же команды и ответы,
данные — в каталоге FAKE_NARRATOR_HOME внутри данных платформы.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from aiplatform.config import Config, pick

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "narrator"  # app/narrator рядом с app/platform
FAKE = Path(__file__).resolve().parent.parent / "fakes" / "fake_narrate.py"
FAKE_HOME_ENV = "FAKE_NARRATOR_HOME"
BRIDGE_TIMEOUT_S = 120  # первый запуск uv собирает окружение — дольше обычного
INSTALL_HINT = "Нужен движок app/narrator: `make models` в нём (или задай AIP_NARRATE_CMD)."


class NarratorError(RuntimeError):
    """Движок ответил ошибкой или не запустился; текст — для человека."""


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
        """`narrate bridge <args>` → ответ JSON. NarratorError — ошибка движка или запуска."""
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
            raise NarratorError(str(payload["error"]))
        if done.returncode != 0 or not isinstance(payload, dict):
            tail = (done.stderr or done.stdout).strip().splitlines()[-3:]
            raise NarratorError("Движок упал: " + (" ".join(tail) or f"код {done.returncode}"))
        return payload


def load(config: Config) -> Narrator:
    home = Path(pick(config.env, "AIP_NARRATOR_DIR", default=str(DEFAULT_DIR))).expanduser()
    if config.fake:
        fake_home = config.data_dir / "fake-narrator"
        return Narrator((sys.executable, str(FAKE)), home, {FAKE_HOME_ENV: str(fake_home)}, fake=True)
    cmd = tuple(shlex.split(pick(config.env, "AIP_NARRATE_CMD", default=str(home / "narrate"))))
    return Narrator(cmd, home)
