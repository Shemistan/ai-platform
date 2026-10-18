"""Настройки вкладки «Диктор»: где SadTalker и его веса (AIP_AVATAR_*)."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from aiplatform import config as platform_config
from aiplatform.config import Config, pick

FAKES = Path(__file__).resolve().parent.parent / "fakes"
# Так его ставит tools/setup-model.sh (ревизия SADTALKER_REVISION); там же он пишет путь в local.mk.
DEFAULT_SADTALKER_HOME = "~/.local/share/avatar/sadtalker-cd4c046"
WEIGHTS_DIR = "SadTalker"  # каталог весов в AIP_MODELS_DIR: checkpoints/ и gfpgan/weights/


@dataclass(frozen=True)
class AvatarConfig:
    data_dir: Path
    backend: str  # "sadtalker" — настоящая модель, "fake" — имитация для демо и тестов
    sadtalker_home: Path  # исходники SadTalker и его .venv
    models_dir: Path
    ffmpeg: str

    @property
    def fake(self) -> bool:
        return self.backend == "fake"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def characters_dir(self) -> Path:
        return self.data_dir / "characters"

    @property
    def weights(self) -> Path:
        return self.models_dir / WEIGHTS_DIR

    def sadtalker_cmd(self) -> tuple[str, ...]:
        """Запуск inference.py из окружения SadTalker; в демо — фейк тем же интерпретатором, что сервис."""
        if self.fake:
            return (sys.executable, str(FAKES / "fake_sadtalker.py"))
        return (str(self.sadtalker_home / ".venv" / "bin" / "python"), str(self.sadtalker_home / "inference.py"))

    def runner_available(self) -> bool:
        return self.fake or all(Path(part).is_file() for part in self.sadtalker_cmd())


def load(config: Config) -> AvatarConfig:
    env = config.env
    explicit_dir = pick(env, "AIP_AVATAR_MODELS_DIR", "AIP_MODELS_DIR")
    return AvatarConfig(
        data_dir=config.data_dir / "avatar",
        backend="fake" if config.fake else "sadtalker",
        sadtalker_home=Path(pick(env, "AIP_AVATAR_SADTALKER_HOME", default=DEFAULT_SADTALKER_HOME)).expanduser(),
        models_dir=Path(explicit_dir or platform_config.DEFAULT_MODELS_DIR).expanduser(),
        ffmpeg=config.ffmpeg,
    )
