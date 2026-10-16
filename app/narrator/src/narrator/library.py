"""Библиотека озвученных статей: каталог на статью и общий реестр index.md.

library/
  index.md                       — таблица всех статей (генерируется, руками не править)
  telegram.json                  — подписчики бота и отметки о рассылке (narrator bot)
  news/news.db                   — база дайджеста новостей
  2026/09/18/                    — год, месяц, день добавления: все статьи этого дня
    speech-synthesis/
      meta.json                  — карточка статьи (схема — Entry)
      source.md                  — извлечённый текст оригинала
      script.txt                 — текст, адаптированный для озвучки
      2026-09-18-…_espeech.mp3   — озвучка, по файлу на движок (_silero, _supertonic)
      check.md                   — отчёт narrator verify (если запускали)
      .cache/                    — клипы абзацев и распознавания, можно удалить
    digest/                      — дайджест новостей дня
  youtube_news/2026/09/          — обзоры для видео на YouTube (narrator news weekly, monthly)
    week_2026-09-19_2026-09-25/  — неделя: первый и последний день, в месяце последнего
    month_2026-09/               — месяц

Имя статьи — «2026-09-18-speech-synthesis»: по нему mp3, отметки рассылки и
дайджестов. От раскладки по папкам оно не зависит.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass, field, fields
from datetime import date
from pathlib import Path

INDEX_NAME = "index.md"
META_NAME = "meta.json"
SOURCE_QUALITIES = ("verbatim", "pasted", "webfetch")
KINDS = ("article", "digest", "weekly", "monthly")
# Обзоры недели и месяца для YouTube: library/youtube_news/week_…/ и month_…/.
YOUTUBE_DIR = "youtube_news"

_TRANSLIT = dict(
    zip(
        "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
        "a b v g d e e zh z i y k l m n o p r s t u f kh ts ch sh sch - y - e yu ya".split(),
        strict=True,
    )
)


@dataclass
class Entry:
    title: str
    added: str
    summary: str = ""
    url: str | None = None
    author: str | None = None
    published: str | None = None
    language: str = "ru"
    source_quality: str = "verbatim"
    audio: str | None = None
    duration_sec: float | None = None
    voice: str | None = None
    engine: str | None = None
    # Все озвученные версии: движок → длительность, файл — audio_name(каталог, движок).
    # audio, duration_sec, voice, engine — последняя озвучка.
    versions: dict[str, float] = field(default_factory=dict)
    # digest — дайджест новостей дня (narrator news): озвучка, канал новостей с обложкой;
    # weekly, monthly — обзоры недели и месяца: текст для суфлёра, видео снимает человек.
    kind: str = "article"
    image_prompt: str = ""  # устарело: промпт обложки теперь в image_prompt.md

    @classmethod
    def load(cls, path: Path) -> Entry:
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{path}: неизвестные поля {sorted(unknown)}")
        entry = cls(**data)
        if entry.source_quality not in SOURCE_QUALITIES:
            raise ValueError(
                f"{path}: source_quality={entry.source_quality!r}, ждём {SOURCE_QUALITIES}"
            )
        if entry.kind not in KINDS:
            raise ValueError(f"{path}: kind={entry.kind!r}, ждём {KINDS}")
        return entry

    def save(self, path: Path) -> None:
        text = json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n"
        path.write_text(text, encoding="utf-8")


LEGACY_AUDIO = "audio.mp3"
_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
_YEAR = re.compile(r"\d{4}")
_TWO = re.compile(r"\d{2}")
_WEEK = re.compile(r"week_\d{4}-\d{2}-\d{2}_(\d{4})-(\d{2})-\d{2}")
_MONTH = re.compile(r"month_(\d{4})-(\d{2})")


def day_dir(root: Path, day: str | date) -> Path:
    """Каталог дня: library/2026/09/18 для «2026-09-18»."""
    y, m, d = str(day)[:10].split("-")
    return root / y / m / d


def youtube_dir(root: Path, day: date) -> Path:
    """Каталог обзоров для YouTube месяца day: library/youtube_news/2026/09."""
    return root / YOUTUBE_DIR / f"{day.year}" / f"{day.month:02d}"


def article_id(path: Path) -> str:
    """«2026-09-18-triple-debt» для library/2026/09/18/triple-debt.

    Обзор для YouTube — «youtube_news-week_…»: его имя уже уникально.
    """
    y, m, d = path.parent.parent.parent.name, path.parent.parent.name, path.parent.name
    if _YEAR.fullmatch(y) and _TWO.fullmatch(m) and _TWO.fullmatch(d):
        return f"{y}-{m}-{d}-{path.name}"
    if YOUTUBE_DIR in path.parts:
        return f"{YOUTUBE_DIR}-{path.name}"
    return f"{path.parent.name}-{path.name}"


def audio_name(path: Path, engine: str) -> str:
    """Имя mp3 статьи для движка: «2026-09-18-triple-debt_silero.mp3».

    День и каталог в имени — чтобы файл, скопированный на телефон, было понятно чей.
    """
    return f"{article_id(path)}_{engine}.mp3"


def _move(old: Path, new: Path) -> None:
    if new.exists():
        return
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(old, new)


def migrate_layout(root: Path) -> None:
    """Раскладка прежних версий → library/<год>/<месяц>/<день>/<статья>/.

    library/2026-09-18-x/ (до папок по дням) и library/2026-09-18/x/ (до
    папок по годам) → library/2026/09/18/x/; library/youtube_news/week_…/
    → library/youtube_news/2026/09/week_…/. Имена статей не меняются.
    """
    for meta in root.glob(f"*/{META_NAME}"):
        old = meta.parent
        day = old.name[:10]
        if _DAY.fullmatch(day) and old.name[10:11] == "-" and old.name[11:]:
            _move(old, day_dir(root, day) / old.name[11:])
    for old_day in root.glob("????-??-??"):
        if not (old_day.is_dir() and _DAY.fullmatch(old_day.name)):
            continue
        for child in list(old_day.iterdir()):
            _move(child, day_dir(root, old_day.name) / child.name)
        if not any(old_day.iterdir()):
            old_day.rmdir()
    for old in list((root / YOUTUBE_DIR).glob("*_*")):
        m = _WEEK.fullmatch(old.name) or _MONTH.fullmatch(old.name)
        if m and old.is_dir():
            _move(old, root / YOUTUBE_DIR / m.group(1) / m.group(2) / old.name)


def migrate_legacy(path: Path, e: Entry) -> None:
    """audio.mp3 из версий до выбора движка → <каталог>_<движок>.mp3."""
    old = path / LEGACY_AUDIO
    if e.audio != LEGACY_AUDIO or not old.exists():
        return
    engine = e.engine or "supertonic"
    new = audio_name(path, engine)
    old.rename(path / new)
    e.audio = new
    e.engine = engine
    if e.duration_sec:
        e.versions.setdefault(engine, e.duration_sec)
    e.save(path / META_NAME)


def library_root() -> Path:
    """$NARRATOR_LIBRARY или library/ движка (app/narrator/) в основном checkout.

    Из git worktree путь ведёт всё равно в основной checkout: библиотека одна
    на машину, а не на ветку.
    """
    env = os.environ.get("NARRATOR_LIBRARY")
    if env:
        return Path(env).expanduser().resolve()
    here = Path(__file__).resolve().parent
    home = here.parents[1]  # app/narrator/ этого checkout
    # Под git-хуком GIT_DIR и GIT_INDEX_FILE уже заданы — без них git смотрит на сам каталог.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:

        def git(*args: str) -> Path:
            out = subprocess.run(
                ["git", "rev-parse", "--path-format=absolute", *args],
                cwd=here,
                capture_output=True,
                text=True,
                check=True,
                env=env,
            )
            return Path(out.stdout.strip())

        top, common = git("--show-toplevel"), git("--git-common-dir")
        return common.parent / home.relative_to(top) / "library"
    except (OSError, subprocess.CalledProcessError, ValueError):
        return home / "library"


def slugify(title: str, limit: int = 60) -> str:
    out = []
    for ch in title.lower():
        if "a" <= ch <= "z" or ch.isdigit():
            out.append(ch)
        elif ch in _TRANSLIT:
            out.append(_TRANSLIT[ch].replace("-", ""))
        else:
            out.append("-")
    slug = re.sub(r"-+", "-", "".join(out)).strip("-")
    return slug[:limit].rstrip("-") or "article"


def create_entry(root: Path, entry: Entry, slug: str | None = None) -> Path:
    """Создаёт library/<год>/<месяц>/<день>/<slug>/ с meta.json; имя занято — суффикс -2."""
    base = slugify(slug or entry.title)
    day = day_dir(root, entry.added)
    path = day / base
    n = 2
    while path.exists():
        path = day / f"{base}-{n}"
        n += 1
    path.mkdir(parents=True)
    entry.save(path / META_NAME)
    return path


def entries(root: Path) -> list[tuple[Path, Entry]]:
    migrate_layout(root)
    metas = [
        *root.glob(f"[0-9][0-9][0-9][0-9]/[0-9][0-9]/[0-9][0-9]/*/{META_NAME}"),
        *root.glob(f"{YOUTUBE_DIR}/*/*/*/{META_NAME}"),
    ]
    found = [(p.parent, Entry.load(p)) for p in metas]
    for path, e in found:
        migrate_legacy(path, e)
    found.sort(key=lambda x: (x[1].added, article_id(x[0])), reverse=True)
    return found


def pending(
    root: Path, engine: str, force: bool = False, kinds: tuple[str, ...] = ("article",)
) -> list[Path]:
    """Каталоги этих видов, которые надо (пере)озвучить этим движком, старые первыми.

    Ждёт озвучки каталог с script.txt, у которого нет mp3 этого движка или он
    старше script.txt. force — все каталоги с script.txt.
    """
    out = []
    for path, e in reversed(entries(root)):
        if e.kind not in kinds:
            continue
        script = path / "script.txt"
        if not script.exists():
            continue
        audio = path / audio_name(path, engine)
        stale = not audio.exists() or audio.stat().st_mtime < script.stat().st_mtime
        if force or stale:
            out.append(path)
    return out


def format_duration(seconds: float | None) -> str:
    if not seconds:
        return "—"
    total = round(seconds)
    h, rest = divmod(total, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _cell(text: str | None) -> str:
    return (text or "—").replace("|", "\\|").replace("\n", " ").strip()


_QUALITY_NOTE = {"verbatim": "", "pasted": "", "webfetch": " ⚠ текст по пересказу WebFetch"}


def render_index(root: Path, items: list[tuple[Path, Entry]]) -> str:
    lines = [
        "# Озвученные статьи",
        "",
        f"Каталог: `{root}`. Файл генерирует `narrator index` — руками не правь,",
        "карточка статьи — `meta.json` в её папке.",
        "",
        "| Добавлена | Статья | О чём | Аудио: движок и длительность | Каталог |",
        "|---|---|---|---|---|",
    ]
    for path, e in items:
        rel = path.relative_to(root).as_posix()
        title = _cell(e.title)
        article = f"[{title}]({e.url})" if e.url else title
        summary = _cell(e.summary) + _QUALITY_NOTE.get(e.source_quality, "")
        links = [
            f"[{eng}]({rel}/{audio_name(path, eng)}) {format_duration(dur)}"
            for eng, dur in sorted(e.versions.items())
            if (path / audio_name(path, eng)).exists()
        ]
        audio = ", ".join(links) or "не озвучена"
        lines.append(f"| {e.added} | {article} | {summary} | {audio} | `{rel}/` |")
    if not items:
        lines.append("| — | — | — | — | — |")
    return "\n".join(lines) + "\n"


def write_index(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / INDEX_NAME
    path.write_text(render_index(root, entries(root)), encoding="utf-8")
    return path


def today() -> str:
    return date.today().isoformat()
