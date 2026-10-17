"""Настройки сценариев: LLM, новости narrator, каналы, рендер."""

from __future__ import annotations

import shlex
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

from aiplatform.audio import config as audio_config
from aiplatform.config import SERVICE_DIR, Config, pick
from aiplatform.studio import publish
from aiplatform.updates import registry

FAKES = Path(__file__).resolve().parent.parent / "fakes"
ASSETS = Path(__file__).resolve().parent / "assets"  # плашка «подпишись» и кнопка «плей», как у narrator
# Модель студии читает чужой текст (статьи, новости) — инструментов ей не даём вовсе:
# --tools "" — ни одного встроенного, --strict-mcp-config — ни одного MCP,
# --setting-sources project — без пользовательских прав и хуков из ~/.claude (SEC-004).
LLM_ISOLATION = "--setting-sources project --strict-mcp-config"
DEFAULT_LLM_CMD = f'claude -p --tools "" {LLM_ISOLATION}'
DEFAULT_IMAGE_MODEL = "z-image-turbo"  # быстрая и лёгкая: картинок на ролик много
# {narrator} и {day} подставляются; команда сбора — как `make news DAY=…` в narrator.
DEFAULT_NEWS_CMD = "make -C {narrator} news-collect news-agent news-judge DAY={day}"
DEFAULT_NEWS_WHERE = "{narrator}/narrate news where --date {day}"


@dataclass(frozen=True)
class StudioConfig:
    data_dir: Path
    fake: bool
    llm_cmd: tuple[str, ...]
    news_cmd: str
    news_where: str
    narrator_dir: Path
    image_model: str
    channels_file: Path
    ffmpeg: str
    font: str
    render_scale: float
    registry_file: Path  # реестр запрещённых организаций: пометки на видео и в посте
    sticker: Path  # плашка «подпишись» (PNG с прозрачностью)
    play: Path  # кнопка «плей» на обложке видео
    schedule_tick_s: float = 30.0  # как часто поток расписания смотрит на часы; 0 — не запускать

    @property
    def projects_dir(self) -> Path:
        return self.data_dir / "projects"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    def channels(self) -> list[publish.Channel]:
        """Каналы публикации; в демо — без каналов narrator (там настоящие каналы Telegram)."""
        return publish.load_channels(self.channels_file, None if self.fake else self.narrator_dir)

    def llm_available(self) -> bool:
        return self.fake or shutil.which(self.llm_cmd[0]) is not None

    def news_argv(self, template: str, day: str) -> list[str]:
        return [part.format(narrator=self.narrator_dir, day=day) for part in shlex.split(template)]


def load(config: Config) -> StudioConfig:
    env = config.env
    data_dir = config.data_dir / "studio"
    if config.fake:
        python = shlex.quote(sys.executable)
        llm_cmd: tuple[str, ...] = (sys.executable, str(FAKES / "fake_llm.py"))
        news_root = shlex.quote(str(data_dir / "fake-news"))
        fake_news = f"{python} {shlex.quote(str(FAKES / 'fake_news.py'))}"
        news_cmd = f"{fake_news} collect --root {news_root} --date {{day}}"
        news_where = f"{fake_news} where --root {news_root} --date {{day}}"
    else:
        llm_cmd = tuple(shlex.split(pick(env, "AIP_LLM_CMD", default=DEFAULT_LLM_CMD)))
        news_cmd = pick(env, "AIP_NEWS_CMD", default=DEFAULT_NEWS_CMD)
        news_where = pick(env, "AIP_NEWS_WHERE", default=DEFAULT_NEWS_WHERE)
    # В демо свой список каналов в данных демо: настоящий channels.json не трогаем.
    # Каналы — данные: в каталоге данных (вне рабочей копии). Старый channels.json рядом
    # с Makefile читается, пока в данных своего нет (перенос — make migrate-data).
    home = config.data_dir
    legacy = SERVICE_DIR / "channels.json"
    if not config.fake and not (home / "channels.json").is_file() and legacy.is_file():
        home = SERVICE_DIR
    channels = pick(env, "AIP_CHANNELS", default=str(home / "channels.json"))
    return StudioConfig(
        data_dir=data_dir,
        fake=config.fake,
        llm_cmd=llm_cmd,
        news_cmd=news_cmd,
        news_where=news_where,
        narrator_dir=audio_config.narrator_dir(config),
        image_model=pick(env, "AIP_STUDIO_IMAGE_MODEL", default=DEFAULT_IMAGE_MODEL),
        channels_file=Path(channels).expanduser(),
        ffmpeg=config.ffmpeg,
        font=config.font,
        render_scale=float(pick(env, "AIP_RENDER_SCALE", default="1")),
        registry_file=config.data_dir / registry.FILE_NAME,
        sticker=Path(pick(env, "AIP_STUDIO_STICKER", default=str(ASSETS / "subscribe.png"))).expanduser(),
        play=Path(pick(env, "AIP_STUDIO_PLAY", default=str(ASSETS / "play.png"))).expanduser(),
        schedule_tick_s=float(pick(env, "AIP_SCHEDULE_TICK_S", default="30")),
    )
