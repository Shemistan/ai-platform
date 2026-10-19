"""Настройки вкладки «Монтаж»: окружение расшифровки, веса, LLM, HyperFrames (AIP_MONTAGE_*)."""

from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.config import Config, pick
from aiplatform.core.settings import Settings

FAKES = Path(__file__).resolve().parent.parent / "fakes"
WORKER = Path(__file__).resolve().parent / "asr_worker.py"
# Так окружение ставит tools/setup-model.sh (версия MLX_AUDIO_VERSION); там же он пишет путь в local.mk.
DEFAULT_ASR_PYTHON = "~/.local/share/montage/mlx-audio-0.5.8/.venv/bin/python"
DEFAULT_HYPERFRAMES_CMD = "~/.local/share/montage/hyperframes-0.8.172/node_modules/.bin/hyperframes"
ASR_DIR = "asr"  # каталог весов расшифровки в AIP_MODELS_DIR
DEFAULT_MAX_GB = 16.0


@dataclass(frozen=True)
class MontageConfig:
    data_dir: Path
    backend: str  # "mlx" — настоящие модели, "fake" — имитация для демо и тестов
    asr_python: Path  # python окружения mlx-audio
    models_dir: Path
    ffmpeg: str
    font: str
    llm_cmd: tuple[str, ...]
    hyperframes_cmd: tuple[str, ...]
    max_upload_bytes: int
    settings: Settings | None = None  # что выключено во вкладке «Настройки»

    @property
    def fake(self) -> bool:
        return self.backend == "fake"

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def lessons_file(self) -> Path:
        return self.data_dir / "lessons.md"

    @property
    def asr_dir(self) -> Path:
        return self.models_dir / ASR_DIR

    def asr_cmd(self) -> tuple[str, ...]:
        """Воркер расшифровки: в окружении mlx-audio; в демо — фейк тем же интерпретатором, что сервис."""
        if self.fake:
            return (sys.executable, str(FAKES / "fake_asr.py"))
        return (str(self.asr_python), str(WORKER))

    def asr_available(self) -> bool:
        return self.fake or self.asr_python.is_file()

    def llm_available(self) -> bool:
        return self.fake or shutil.which(self.llm_cmd[0]) is not None

    def hyperframes_available(self) -> bool:
        return self.fake or shutil.which(self.hyperframes_cmd[0]) is not None

    def disabled_models(self) -> set[str]:
        """Модели расшифровки, выключенные во вкладке «Настройки»."""
        return self.settings.disabled("asr") if self.settings else set()


def load(config: Config) -> MontageConfig:
    # LLM — та же команда, что у студии (claude -p без инструментов, SEC-004): модуль не тянет студию целиком.
    from aiplatform.studio import config as studio_config

    env = config.env
    models_dir = pick(env, "AIP_MONTAGE_MODELS_DIR", "AIP_MODELS_DIR") or platform_config.DEFAULT_MODELS_DIR
    if config.fake:
        llm_cmd: tuple[str, ...] = (sys.executable, str(FAKES / "fake_llm.py"))
        hyperframes: tuple[str, ...] = (sys.executable, str(FAKES / "fake_hyperframes.py"))
    else:
        llm_cmd = tuple(shlex.split(pick(env, "AIP_LLM_CMD", default=studio_config.DEFAULT_LLM_CMD)))
        raw = pick(env, "AIP_MONTAGE_HYPERFRAMES_CMD", default=DEFAULT_HYPERFRAMES_CMD)
        parts = shlex.split(raw)
        hyperframes = (str(Path(parts[0]).expanduser()), *parts[1:])
    try:
        max_gb = float(pick(env, "AIP_MONTAGE_MAX_GB", default=str(DEFAULT_MAX_GB)))
    except ValueError:
        max_gb = DEFAULT_MAX_GB
    return MontageConfig(
        data_dir=config.data_dir / "montage",
        backend="fake" if config.fake else "mlx",
        asr_python=Path(pick(env, "AIP_MONTAGE_ASR_PYTHON", default=DEFAULT_ASR_PYTHON)).expanduser(),
        models_dir=Path(models_dir).expanduser(),
        ffmpeg=config.ffmpeg,
        font=config.font,
        llm_cmd=llm_cmd,
        hyperframes_cmd=hyperframes,
        max_upload_bytes=int(max(0.1, max_gb) * 2**30),
        settings=config.settings,
    )
