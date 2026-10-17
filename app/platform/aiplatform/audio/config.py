"""Настройки вкладки «Аудио»: чем озвучивать (движок narrator) и где хранить."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aiplatform.audio import voices
from aiplatform.config import Config, pick
from aiplatform.core import narrator as narrator_module
from aiplatform.core.narrator import Narrator
from aiplatform.core.settings import Settings


@dataclass(frozen=True)
class AudioConfig:
    data_dir: Path
    fake: bool
    narrator: Narrator
    preferred_engine: str  # AIP_AUDIO_ENGINE / NARRATOR_ENGINE
    ffmpeg: str
    settings: Settings | None = None  # что выключено во вкладке «Настройки»
    explicit: bool = False  # движок задан переменной, а не взят по умолчанию
    # Движки с установленными весами (мост engines); None — неизвестно.
    installed: Callable[[], set[str] | None] | None = None

    def engines(self) -> dict[str, voices.Engine]:
        """Движки и голоса без выключенных в настройках."""
        return voices.enabled(self.settings)

    @property
    def default_engine(self) -> str:
        """Движок по умолчанию.

        Заданный переменной и не выключенный — он. Иначе первый включённый с
        установленными весами в порядке FALLBACK_ORDER, затем любой с весами:
        после одного `make models` озвучка запускается и без higgs. Веса
        неизвестны (мост не ответил) — DEFAULT_ENGINE или первый включённый.
        """
        engines = self.engines()
        if self.explicit and self.preferred_engine in engines:
            return self.preferred_engine
        ready = self.installed() if self.installed else None
        if ready:
            for engine in (*voices.FALLBACK_ORDER, *engines):
                if engine in engines and engine in ready:
                    return engine
        for engine in (self.preferred_engine, voices.DEFAULT_ENGINE):
            if engine in engines:
                return engine
        return next(iter(engines), self.preferred_engine)

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
    engine = pick(config.env, "AIP_AUDIO_ENGINE", "NARRATOR_ENGINE", default="")
    explicit = engine in voices.ENGINES
    if not explicit:
        engine = voices.DEFAULT_ENGINE
    return AudioConfig(
        data_dir=config.data_dir / "audio",
        fake=config.fake,
        narrator=narrator_module.load(config),
        preferred_engine=engine,
        ffmpeg=config.ffmpeg,
        settings=config.settings,
        explicit=explicit,
    )
