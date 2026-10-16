"""Настройки вкладки «Аудио»: чем озвучивать (движок narrator) и где хранить."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiplatform.audio import voices
from aiplatform.config import Config, pick
from aiplatform.core import narrator as narrator_module
from aiplatform.core.narrator import Narrator


@dataclass(frozen=True)
class AudioConfig:
    data_dir: Path
    fake: bool
    narrator: Narrator
    default_engine: str
    ffmpeg: str

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def narrate_cmd(self) -> tuple[str, ...]:
        return self.narrator.cmd

    @property
    def narrator_dir(self) -> Path:
        return self.narrator.home

    def runner_available(self) -> bool:
        return self.narrator.available()


def narrator_dir(config: Config) -> Path:
    return narrator_module.load(config).home


def load(config: Config) -> AudioConfig:
    engine = pick(config.env, "AIP_AUDIO_ENGINE", "NARRATOR_ENGINE", default=voices.DEFAULT_ENGINE)
    if engine not in voices.ENGINES:
        engine = voices.DEFAULT_ENGINE
    return AudioConfig(
        data_dir=config.data_dir / "audio",
        fake=config.fake,
        narrator=narrator_module.load(config),
        default_engine=engine,
        ffmpeg=config.ffmpeg,
    )
