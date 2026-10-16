"""Источники дайджеста: config/news-sources.toml → список Source.

Путь к конфигу можно подменить `$NARRATOR_NEWS_SOURCES`.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

SOURCES_ENV = "NARRATOR_NEWS_SOURCES"
KINDS = ("feed", "page", "github", "godl", "youtube", "telegram", "govuln", "ghsa")
# Без какого поля источник этого вида не забрать.
REQUIRED = {
    "feed": "feed",
    "page": "match",
    "github": "repo",
    "youtube": "handle",
    "telegram": "channel",
    "ghsa": "ecosystem",
}


class ConfigError(ValueError):
    """Конфиг источников не читается или противоречив."""


@dataclass
class Source:
    id: str
    name: str
    url: str
    type: str
    category: str
    kind: str
    priority: int = 3
    authority: float = 0.0  # 0 — вес типа из [types]
    secondary: bool = (
        False  # вторичный: без подтверждения важность не выше 6; true — и вне [confirm]
    )
    max_importance: int = 10  # потолок важности: пилотный канал не идёт в главное
    enabled: bool = True
    note: str = ""
    feed: str | None = None
    match: str | None = None
    repo: str | None = None
    tags: bool = False
    exclude: str | None = None
    handle: str | None = None
    channel_id: str | None = None  # YouTube: если задан, handle не резолвится
    channel: str | None = None
    ecosystem: str | None = None  # ghsa: go, actions, …
    min_severity: str | None = None  # ghsa: low, medium, high, critical — не ниже
    packages: list[str] | None = None  # ghsa: только эти пакеты (параметр affects)
    vendor: str | None = None  # чьи новости: clickhouse_blog и clickhouse_github — clickhouse

    @property
    def vendor_key(self) -> str:
        """Поставщик для правила разнообразия: vendor или id до первого «_»."""
        return self.vendor or self.id.split("_")[0]


def config_path() -> Path:
    env = os.environ.get(SOURCES_ENV)
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3] / "config" / "news-sources.toml"


def load(path: Path | None = None) -> list[Source]:
    """Все источники конфига, включая выключенные; ConfigError — если он кривой."""
    path = path or config_path()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ConfigError(f"{path}: {e}") from e
    types: dict[str, float] = data.get("types", {})
    secondary = set(data.get("confirm", {}).get("secondary", []))
    known = {f.name for f in fields(Source)}
    out: list[Source] = []
    seen: set[str] = set()
    for raw in data.get("source", []):
        where = f"{path}: источник {raw.get('id', '?')}"
        unknown = set(raw) - known
        if unknown:
            raise ConfigError(f"{where}: неизвестные поля {sorted(unknown)}")
        try:
            src = Source(**raw)
        except TypeError as e:
            raise ConfigError(f"{where}: {e}") from e
        if not re.fullmatch(r"[a-z0-9_]+", src.id):
            raise ConfigError(f"{where}: id — строчная латиница, цифры и _")
        if src.id in seen:
            raise ConfigError(f"{where}: id повторяется")
        seen.add(src.id)
        if src.type not in types:
            raise ConfigError(f"{where}: type={src.type!r}, ждём одно из {sorted(types)}")
        if src.kind not in KINDS:
            raise ConfigError(f"{where}: kind={src.kind!r}, ждём одно из {KINDS}")
        need = REQUIRED.get(src.kind)
        if need and not getattr(src, need):
            raise ConfigError(f"{where}: для kind={src.kind} нужно поле {need}")
        src.authority = src.authority or types[src.type]
        src.secondary = src.secondary or src.type in secondary
        out.append(src)
    return out


def by_id(sources: list[Source]) -> dict[str, Source]:
    return {s.id: s for s in sources}
