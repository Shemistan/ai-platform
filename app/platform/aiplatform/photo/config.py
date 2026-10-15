"""Настройки вкладки «Фото»: AIP_PHOTO_*, с откатом на PHOTOGEN_* из local.mk photogen."""

from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.config import Config, pick
from aiplatform.photo import models

DEFAULT_FLUX2_CMD = "Flux2CLI"
FAKE_GEN = Path(__file__).resolve().parent.parent / "fakes" / "fake_gen.py"


@dataclass(frozen=True)
class PhotoConfig:
    data_dir: Path
    backend: str  # "real" | "fake"
    mflux_bin: Path | None  # каталог bin окружения с командами mflux-generate-*
    flux2_cmd: tuple[str, ...]
    models_dir: Path | None
    low_ram: bool

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def fake(self) -> bool:
        return self.backend == "fake"

    def mflux_cmd(self, command: str) -> tuple[str, ...]:
        """Чем запустить команду mflux: из окружения setup-model.sh, иначе из PATH."""
        if self.fake:
            return (*_fake_cmd(), command)
        return (str(self.mflux_bin / command) if self.mflux_bin else command,)

    def flux2(self) -> tuple[str, ...]:
        return (*_fake_cmd(), "flux2") if self.fake else self.flux2_cmd

    def runner_available(self, backend: str) -> bool:
        if self.fake:
            return True
        if backend == models.FLUX2:
            return shutil.which(self.flux2_cmd[0]) is not None
        probe = "mflux-generate-qwen"
        return shutil.which(str(self.mflux_bin / probe) if self.mflux_bin else probe) is not None

    def models(self) -> list[models.Available]:
        return models.resolve(models_dir=self.models_dir, fake=self.fake)


def _fake_cmd() -> tuple[str, ...]:
    return (sys.executable, str(FAKE_GEN))


def load(config: Config) -> PhotoConfig:
    env = config.env
    flux2_cmd = tuple(shlex.split(pick(env, "AIP_PHOTO_FLUX2_CMD", "PHOTOGEN_FLUX2_CMD", default=DEFAULT_FLUX2_CMD)))
    mflux_bin = pick(env, "AIP_PHOTO_MFLUX_BIN", "PHOTOGEN_MFLUX_BIN")
    models_dir = pick(env, "AIP_MODELS_DIR", "PHOTOGEN_MODELS_DIR", default=platform_config.DEFAULT_MODELS_DIR)
    return PhotoConfig(
        data_dir=config.data_dir / "photo",
        backend=config.backend,
        mflux_bin=Path(mflux_bin).expanduser() if mflux_bin else None,
        flux2_cmd=flux2_cmd,
        models_dir=Path(models_dir).expanduser(),
        low_ram=platform_config.flag(env, "AIP_LOW_RAM", "PHOTOGEN_LOW_RAM"),
    )
