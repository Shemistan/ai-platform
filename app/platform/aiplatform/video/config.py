"""Настройки вкладки «Видео»: AIP_VIDEO_*, с откатом на VIDEOGEN_* из local.mk videogen."""

from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.config import Config, pick
from aiplatform.video import models

DEFAULT_LTX_CMD = "ltx-2-mlx"
DEFAULT_WAN_CMD = "pocketai-wan-video"
DEFAULT_WAN_STEPS = 10  # настройка, с которой сборку весов проверял её автор
FAKES = Path(__file__).resolve().parent.parent / "fakes"


@dataclass(frozen=True)
class VideoConfig:
    data_dir: Path
    backend: str  # "ltx" — настоящая модель, "fake" — имитация для демо и тестов
    ltx_cmd: tuple[str, ...]
    wan_cmd: tuple[str, ...]
    wan_steps: int
    model: str  # VIDEOGEN_MODEL как задано; пусто — брать модели из models_dir
    models_dir: Path | None
    low_ram: bool

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def fake(self) -> bool:
        return self.backend == "fake"

    def runner_available(self) -> bool:
        return shutil.which(self.ltx_cmd[0]) is not None

    def gemma(self) -> str | None:
        """Каталог текстового энкодера рядом с весами; None — пайплайн возьмёт его из кэша Hugging Face."""
        local = self.models_dir / models.GEMMA_DIR if self.models_dir else None
        return str(local) if local and local.is_dir() else None

    def models(self) -> list[models.Available]:
        return models.resolve(models_dir=self.models_dir, explicit=self.model, fake=self.fake)


def load(config: Config) -> VideoConfig:
    env = config.env
    if config.fake:
        backend = "fake"
        ltx_cmd = (sys.executable, str(FAKES / "fake_ltx.py"))
        wan_cmd = (sys.executable, str(FAKES / "fake_wan.py"))
    else:
        backend = "ltx"
        ltx_cmd = tuple(shlex.split(pick(env, "AIP_VIDEO_LTX_CMD", "VIDEOGEN_LTX_CMD", default=DEFAULT_LTX_CMD)))
        wan_cmd = tuple(shlex.split(pick(env, "AIP_VIDEO_WAN_CMD", "VIDEOGEN_WAN_CMD", default=DEFAULT_WAN_CMD)))
    wan_steps = int(pick(env, "AIP_VIDEO_WAN_STEPS", "VIDEOGEN_WAN_STEPS", default=str(DEFAULT_WAN_STEPS)))
    if wan_steps < 1:
        raise ValueError("AIP_VIDEO_WAN_STEPS: нужно целое число от 1")
    model = pick(env, "AIP_VIDEO_MODEL", "VIDEOGEN_MODEL")
    # Каталог со всеми весами: задан явно либо это сосед каталога из VIDEOGEN_MODEL —
    # так модель, доустановленная ./setup-model.sh, появляется в списке сама.
    models_dir: Path | None
    explicit_dir = pick(env, "AIP_VIDEO_MODELS_DIR", "VIDEOGEN_MODELS_DIR", "AIP_MODELS_DIR")
    if explicit_dir:
        models_dir = Path(explicit_dir).expanduser()
    elif model and Path(model).expanduser().is_dir():
        models_dir = Path(model).expanduser().parent
    else:
        models_dir = Path(platform_config.DEFAULT_MODELS_DIR)
    return VideoConfig(
        data_dir=config.data_dir / "video",
        backend=backend,
        ltx_cmd=ltx_cmd,
        wan_cmd=wan_cmd,
        wan_steps=wan_steps,
        model=model,
        models_dir=models_dir,
        low_ram=platform_config.flag(env, "AIP_LOW_RAM", "VIDEOGEN_LOW_RAM"),
    )
