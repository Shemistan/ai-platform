"""Общие настройки платформы из переменных окружения (AIP_*).

Настройки вкладок читают свои модули (photo/config.py и т.д.) из того же
окружения: у фото и видео есть откат на старые имена PHOTOGEN_* / VIDEOGEN_*,
чтобы local.mk от setup-model.sh исходных сервисов продолжал работать.
"""

from __future__ import annotations

import ipaddress
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from aiplatform.core import settings as settings_module
from aiplatform.core.settings import Settings

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
    auth_token: str = field(default="", repr=False)  # AIP_AUTH_TOKEN: доступ не с loopback (SEC-007)
    allowed_hosts: tuple[str, ...] = ()  # AIP_ALLOWED_HOSTS: свои имена в Host помимо loopback (SEC-002)

    @property
    def fake(self) -> bool:
        return self.backend == "fake"

    @property
    def uploads_dir(self) -> Path:
        return self.data_dir / "uploads"

    @property
    def settings(self) -> Settings:
        return settings_module.at(self.data_dir)


def default_font() -> str:
    return next((f for f in FONT_CANDIDATES if Path(f).is_file()), "")


# Выставляет tests/__init__.py (и наследуют подпроцессы тестов): под ним боевые
# каталоги данных запрещены — тест не должен читать чужие настройки и портить их.
TEST_GUARD_ENV = "AIP_TEST_GUARD"
LEGACY_DATA_DIR = SERVICE_DIR / "data"  # до REL-012 данные жили в рабочей копии
SERVICE_DATA_DIRS = (LEGACY_DATA_DIR, SERVICE_DIR / "data-demo")
# Данные вне рабочей копии: чистка, ветки и worktree агента их не трогают.
USER_DATA_DIR = "Library/Application Support/aiplatform"


def user_data_dir(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or Path.home()) / USER_DATA_DIR


def default_data_dir(env: Mapping[str, str], legacy: Path = LEGACY_DATA_DIR) -> Path:
    """AIP_DATA_DIR не задан: ~/Library/Application Support/aiplatform; пока его нет, а старый
    app/platform/data есть — старый (перенос — make migrate-data, см. README)."""
    new = user_data_dir(env)
    if not new.exists() and legacy.is_dir():
        return legacy
    return new


def missing_volume(path: Path) -> Path | None:
    """Путь на внешнем томе (/Volumes/<том>/…), а том не подключён — сам том; иначе None.

    Без проверки сервис на отключённом SSD создал бы пустой каталог данных и работал «с нуля».
    """
    parts = Path(path).expanduser().parts
    if len(parts) < 3 or parts[:2] != ("/", "Volumes"):
        return None
    volume = Path("/Volumes", parts[2])
    return None if os.path.ismount(volume) else volume


def _guard_tests(data_dir: Path) -> None:
    if not flag(os.environ, TEST_GUARD_ENV):
        return
    guarded = {d.resolve() for d in SERVICE_DATA_DIRS} | {user_data_dir(os.environ).resolve()}
    if data_dir in guarded:
        raise RuntimeError(
            f"тесты не должны работать с боевым каталогом данных {data_dir}: "
            "задай AIP_DATA_DIR во временный каталог (tests.helpers.isolated_config)"
        )


class ConfigError(ValueError):
    """Настройки не дают безопасно стартовать: сервер не запускается."""


_AUTH_TOKEN = re.compile(r"[A-Za-z0-9._~-]{16,}")


def is_loopback(host: str) -> bool:
    """127.0.0.1, ::1, localhost — сервер виден только с этой машины."""
    host = host.strip().strip("[]").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _auth(host: str, token: str) -> str:
    if token and not _AUTH_TOKEN.fullmatch(token):
        raise ConfigError(
            "AIP_AUTH_TOKEN — от 16 символов из латиницы, цифр и ._~-: "
            'python3 -c "import secrets; print(secrets.token_urlsafe(32))"'
        )
    if not is_loopback(host) and not token:
        raise ConfigError(
            f"AIP_HOST={host or '(все адреса)'} открывает платформу в сеть, а аутентификации без "
            "AIP_AUTH_TOKEN нет: любой в сети сможет публиковать в каналы и запускать агентов. "
            "Задай AIP_AUTH_TOKEN или верни AIP_HOST=127.0.0.1 и ходи через SSH-туннель или Tailscale."
        )
    return token


def load(env: Mapping[str, str] | None = None) -> Config:
    env = dict(os.environ if env is None else env)
    backend = pick(env, "AIP_BACKEND", default="real")
    if backend not in ("real", "fake"):
        raise ValueError(f"AIP_BACKEND: ожидается real или fake, получено {backend!r}")
    data = pick(env, "AIP_DATA_DIR", default=str(default_data_dir(env)))
    data_dir = Path(data).expanduser()
    if not data_dir.is_absolute():
        data_dir = SERVICE_DIR / data_dir
    if volume := missing_volume(data_dir):
        raise ConfigError(f"каталог данных {data_dir} — на диске {volume}, а он не подключён: подключи диск")
    data_dir = data_dir.resolve()
    _guard_tests(data_dir)
    host = pick(env, "AIP_HOST", default="127.0.0.1").strip()
    return Config(
        auth_token=_auth(host, pick(env, "AIP_AUTH_TOKEN").strip()),
        allowed_hosts=tuple(h.strip().lower() for h in pick(env, "AIP_ALLOWED_HOSTS").split(",") if h.strip()),
        data_dir=data_dir,
        host=host,
        port=int(pick(env, "AIP_PORT", default=str(DEFAULT_PORT))),
        backend=backend,
        ffmpeg=pick(env, "AIP_FFMPEG", default="ffmpeg"),
        font=pick(env, "AIP_FONT", default=default_font()),
        env=env,
    )
