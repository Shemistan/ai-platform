"""Настройки вкладки «Аудио»: чем озвучивать (narrator CLI) и где хранить."""

from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from aiplatform.audio import voices
from aiplatform.config import Config, pick

DEFAULT_NARRATOR_DIR = Path.home() / "my_track" / "07_offline_text-to-word"
FAKE_NARRATE = Path(__file__).resolve().parent.parent / "fakes" / "fake_narrate.py"


@dataclass(frozen=True)
class AudioConfig:
    data_dir: Path
    fake: bool
    narrate_cmd: tuple[str, ...]
    narrator_dir: Path
    default_engine: str
    ffmpeg: str

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    def runner_available(self) -> bool:
        return self.fake or shutil.which(self.narrate_cmd[0]) is not None


def narrator_dir(config: Config) -> Path:
    return Path(pick(config.env, "AIP_NARRATOR_DIR", default=str(DEFAULT_NARRATOR_DIR))).expanduser()


def load(config: Config) -> AudioConfig:
    env = config.env
    home = narrator_dir(config)
    if config.fake:
        cmd: tuple[str, ...] = (sys.executable, str(FAKE_NARRATE))
    else:
        cmd = tuple(shlex.split(pick(env, "AIP_NARRATE_CMD", default=str(home / "narrate"))))
    engine = pick(env, "AIP_AUDIO_ENGINE", "NARRATOR_ENGINE", default=voices.DEFAULT_ENGINE)
    if engine not in voices.ENGINES:
        engine = voices.DEFAULT_ENGINE
    return AudioConfig(
        data_dir=config.data_dir / "audio",
        fake=config.fake,
        narrate_cmd=cmd,
        narrator_dir=home,
        default_engine=engine,
        ffmpeg=config.ffmpeg,
    )
