"""Общие настройки платформы из переменных окружения (AIP_*).

Настройки вкладок читают свои модули (photo/config.py и т.д.) из того же
окружения: у фото и видео есть откат на старые имена PHOTOGEN_* / VIDEOGEN_*,
чтобы local.mk от setup-model.sh исходных сервисов продолжал работать.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PORT = 8780
DEFAULT_MODELS_DIR = "/Volumes/External SSD/local_models"
FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)
PACKAGE_DIR = Path(__file__).resolve().parent
SERVICE_DIR = PACKAGE_DIR.parent  # app/platform


def pick(env: Mapping[str, str], *names: str, default: str = "") -> str:
    """Первое непустое значение из имён по порядку; пусто = не задано."""
    for name in names:
        value = env.get(name)
        if value:
            return value
    return default


def flag(env: Mapping[str, str], *names: str) -> bool:
    return pick(env, *names) not in ("", "0", "false", "no")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    host: str
    port: int
    backend: str  # "real" — настоящие модели и инструменты, "fake" — имитация для демо и тестов
    ffmpeg: str
    font: str
    env: Mapping[str, str] = field(default_factory=dict, repr=False)

    @property
    def fake(self) -> bool:
        return self.backend == "fake"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"


def default_font() -> str:
    return next((f for f in FONT_CANDIDATES if Path(f).is_file()), "")


def load(env: Mapping[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    backend = pick(env, "AIP_BACKEND", default="real")
    if backend not in ("real", "fake"):
        raise ValueError(f"AIP_BACKEND: ожидается real или fake, получено {backend!r}")
    data = pick(env, "AIP_DATA_DIR", default=str(SERVICE_DIR / "data"))
    data_dir = Path(data).expanduser()
    if not data_dir.is_absolute():
        data_dir = SERVICE_DIR / data_dir
    return Config(
        data_dir=data_dir.resolve(),
        host=pick(env, "AIP_HOST", default="127.0.0.1"),
        port=int(pick(env, "AIP_PORT", default=str(DEFAULT_PORT))),
        backend=backend,
        ffmpeg=pick(env, "AIP_FFMPEG", default="ffmpeg"),
        font=pick(env, "AIP_FONT", default=default_font()),
        env=env,
    )
