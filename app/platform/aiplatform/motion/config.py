"""Настройки вкладки «Моушн»: свой каталог данных, параллельность агентов; расшифровка, LLM и HyperFrames —
те же, что у «Монтажа» (AIP_MONTAGE_*, AIP_LLM_CMD)."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from aiplatform.config import Config, pick
from aiplatform.montage import config as montage_config
from aiplatform.montage.config import MontageConfig

DEFAULT_PARALLEL = 3


@dataclass(frozen=True)
class MotionConfig:
    data_dir: Path
    montage: MontageConfig  # расшифровка, LLM, HyperFrames, ffmpeg, предел загрузки
    parallel: int  # сцен одновременно у агентов (рендер — по одной)

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def montage_projects_dir(self) -> Path:
        return self.montage.projects_dir

    @property
    def ffmpeg(self) -> str:
        return self.montage.ffmpeg

    @property
    def fake(self) -> bool:
        return self.montage.fake

    def node(self) -> str | None:
        """node для проверки синтаксиса кода аниматора: тот, что запускает HyperFrames, иначе из PATH."""
        first = Path(self.montage.hyperframes_cmd[0])
        if first.name == "node" and first.is_file():
            return str(first)
        return shutil.which("node")


def load(config: Config) -> MotionConfig:
    try:
        parallel = int(pick(config.env, "AIP_MOTION_PARALLEL", default=str(DEFAULT_PARALLEL)))
    except ValueError:
        parallel = DEFAULT_PARALLEL
    return MotionConfig(
        data_dir=config.data_dir / "motion",
        montage=montage_config.load(config),
        parallel=min(8, max(1, parallel)),
    )
