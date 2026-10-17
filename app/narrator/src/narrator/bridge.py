"""`narrate bridge …` — данные для страниц платформы: каждая команда печатает JSON.

Человеку CLI печатает текст; страницам нужны те же сведения структурой. Мост
ничего не спрашивает и не печатает лишнего: в stdout — один JSON-объект.
Ошибку, понятную человеку, он отдаёт полем `error` с кодом выхода 1. Текст
на вход (`preview`, `split`, `article-new`, `article-save`) — через stdin.

Долгие действия (озвучка, сбор, рассылка) мост не делает: их платформа
запускает обычными командами (`synth`, `news collect`, `bot send` …) и
показывает их вывод как лог задачи.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path

from narrator import library, script
from narrator.library import META_NAME, Entry

# Версия контракта моста. Платформа сверяет её (`bridge version`) и при
# несовпадении показывает баннер вместо мёртвых страниц. Меняй вместе с
# EXPECTED_BRIDGE_API в app/platform/aiplatform/core/narrator.py.
BRIDGE_API = 1
SCRIPT = "script.txt"
SOURCE = "source.md"
CHECK = "check.md"
MAX_BATCH_ARTICLES = 100
TITLE_CHARS = 90
STEPS_RANGE = (1, 128)
SPEED_RANGE = (0.7, 2.0)
REVIEW_FILES = ("teleprompter.md", "materials.md", "post.md", "events.md")
_OVERALL = re.compile(r"Совпадение по словам: (\d+)%")
_FRONT_MATTER = re.compile(r"\A---[ \t]*\n.*?\n---[ \t]*\n", re.DOTALL)
_FENCE = re.compile(r"^\s*(```|~~~)")
_H1 = re.compile(r"^#\s+\S")
_RULE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")


class BridgeError(Exception):
    """Ошибка для человека: уходит полем error, тип — полем kind.

    kind: invalid — неверный запрос (по умолчанию), not_found — нет такого объекта,
    unavailable — движку не хватает зависимости (команда упала, нет сети/модели).
    Платформа выбирает HTTP-код по kind, а не по тексту.
    """

    def __init__(self, message: str, kind: str = "invalid") -> None:
        super().__init__(message)
        self.kind = kind


# --- текст ---------------------------------------------------------------


def preview(text: str) -> dict:
    """Каким текст уйдёт в модель: сам текст, число изменённых строк и замечания."""
    from narrator import normalize

    if not text.strip():
        return {"script": "", "changed": 0, "issues": []}
    spoken = normalize.normalize(text)
    issues = [
        {"line": i.line, "col": i.col, "level": "warning", "code": i.code, "message": i.message}
        for i in script.lint(spoken)
        if i.code != "EMPTY"
    ]
    return {"script": spoken, "changed": normalize.changed(text, spoken), "issues": issues}


def split_articles(text: str) -> list[str]:
    """Markdown-файл → статьи.

    Статью открывает заголовок первого уровня (`# …`); текст до первого
    заголовка — отдельная статья. Таких заголовков меньше двух — границей
    служит линия `---` после пустой строки. Нет и её — весь файл одна статья.
    Внутри блоков кода границы не ищутся, YAML-шапка файла отбрасывается.
    """
    lines = _FRONT_MATTER.sub("", text.replace("\r\n", "\n"), count=1).split("\n")
    heads: list[int] = []
    rules: list[int] = []
    fenced = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            fenced = not fenced
        elif fenced:
            continue
        elif _H1.match(line):
            heads.append(i)
        elif _RULE.match(line) and (i == 0 or not lines[i - 1].strip()):
            rules.append(i)
    if len(heads) > 1:
        bounds = [0, *heads, len(lines)]
        parts = [lines[a:b] for a, b in zip(bounds, bounds[1:], strict=False)]
    else:
        bounds = [-1, *rules, len(lines)]
        parts = [lines[a + 1 : b] for a, b in zip(bounds, bounds[1:], strict=False)]
    joined = ("\n".join(part).strip() for part in parts)
    return [part + "\n" for part in joined if part]


def article_title(text: str) -> str:
    """Первая непустая строка статьи без решёток заголовка."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    return first.strip().lstrip("#").strip()[:TITLE_CHARS]


def split(text: str) -> dict:
    parts = split_articles(text)
    if len(parts) > MAX_BATCH_ARTICLES:
        raise BridgeError(f"В файле {len(parts)} статей, за раз — не больше {MAX_BATCH_ARTICLES}.")
    return {
        "articles": [{"title": article_title(p), "words": len(p.split()), "text": p} for p in parts]
    }


# --- движки, голоса, модели ---------------------------------------------


def engine_labels() -> dict[str, tuple[str, str]]:
    from narrator import mlx_engines

    return {
        "espeech": ("ESpeech", "лучшее качество, нужна видеокарта"),
        "silero": ("Silero", "секунды на CPU, свои ударения"),
        "supertonic": ("Supertonic", "быстро на CPU"),
    } | {
        e.id: (e.label, f"{e.note}, около {e.size_gb:g} ГБ")
        for e in mlx_engines.MLX_ENGINES.values()
    }


def engine_problem(engine: str) -> str | None:
    """Почему движок не запустится; None — всё на месте."""
    import importlib.util

    from narrator import mlx_engines, stress
    from narrator import synth as s

    accents = None if (stress.ruaccent_dir() / "dictionary").exists() else "нет моделей RUAccent"
    if engine in mlx_engines.MLX_ENGINES:
        return mlx_engines.problem(engine)
    if engine == "silero":
        return None if s.silero_path().exists() else "нет модели Silero"
    if engine == "supertonic":
        return accents if (s.supertonic_dir() / "onnx").exists() else "нет модели Supertonic"
    if importlib.util.find_spec("f5_tts") is None:
        return "не установлен f5-tts"
    if not (s.espeech_dir() / "espeech_tts_rlv2.pt").exists():
        return "нет модели ESpeech"
    hf_vocos = Path("~/.cache/huggingface/hub/models--charactr--vocos-mel-24khz").expanduser()
    if not (s.vocos_dir() / "config.yaml").exists() and not hf_vocos.exists():
        return "нет вокодера vocos"
    return accents


def verify_available() -> bool:
    from narrator import verify

    hf = Path("~/.cache/huggingface/hub/models--Systran--faster-whisper-small").expanduser()
    return (verify.whisper_dir() / "model.bin").exists() or hf.exists()


def engines() -> dict:
    from narrator import models
    from narrator import synth as s

    labels = engine_labels()
    out = []
    for engine in s.ENGINES:
        label, note = labels[engine]
        steps, speed = s.ENGINE_DEFAULTS[engine]
        out.append(
            {
                "id": engine,
                "label": label,
                "note": note,
                # Голос-клон без записанного образца не предлагаем.
                "voices": [v for v in s.ENGINE_VOICES[engine] if s.has_voice(engine, v)],
                "default_voice": s.DEFAULT_VOICES[engine],
                "default_steps": steps or None,
                "default_speed": speed,
                "tunable": engine in s.TUNABLE_ENGINES,
                "problem": engine_problem(engine),
            }
        )
    return {
        "engines": out,
        "verify_available": verify_available(),
        "models_dir": str(models.models_root()),
        "models_dir_exists": models.models_root().exists(),
        "models_hint": models.missing_hint(),
        "steps_range": list(STEPS_RANGE),
        "speed_range": list(SPEED_RANGE),
    }


def voices() -> dict:
    """Образцы голосов-клонов: какой записан, где, сколько секунд, что сказано."""
    import soundfile as sf

    from narrator import synth as s
    from narrator.stress import strip_marks

    out = []
    for voice, stem in s.CLONE_VOICES.items():
        item = {
            "voice": voice,
            "stem": stem,
            # M1 — образец из Supertonic, его не перезаписывают.
            "record": None if voice == "M1" else ("gemini" if voice in s.GEMINI_VOICES else "file"),
            "sample": None,
            "seconds": None,
            "text": "",
            "custom": False,
        }
        try:
            wav, text = s.ref_sample(voice)
        except s.ModelMissingError:
            out.append(item)
            continue
        try:
            seconds = round(sf.info(str(wav)).duration, 1)
        except RuntimeError:
            seconds = None
        item |= {
            "sample": str(wav),
            "seconds": seconds,
            "text": strip_marks(text.read_text(encoding="utf-8")).strip(),
            "custom": wav.parent == s.voices_dir(),
        }
        out.append(item)
    low, high = s.REF_SECONDS
    return {
        "voices": out,
        "seconds_range": [low, high],
        "default_text": strip_marks(s.REF_TEXT.read_text(encoding="utf-8")).strip(),
        "voices_dir": str(s.voices_dir()),
    }


def models_view() -> dict:
    """Модели озвучки на диске и модели mlx для сравнения."""
    from narrator import mlx_engines, models, stress, verify
    from narrator import synth as s

    main = [
        ("espeech", "ESpeech-TTS-1", s.espeech_dir()),
        ("vocos", "Вокодер vocos для ESpeech", s.vocos_dir()),
        ("ruaccent", "Ударения RUAccent", stress.ruaccent_dir()),
        ("supertonic", "Supertonic 3", s.supertonic_dir()),
        ("silero", "Silero v5.5", s.silero_path()),
        ("whisper", "Проверка распознаванием: faster-whisper small", verify.whisper_dir()),
    ]
    return {
        "root": str(models.models_root()),
        "root_exists": models.models_root().exists(),
        "hint": models.missing_hint(),
        "main": [
            {"id": i, "label": label, "path": str(p), "exists": p.exists()} for i, label, p in main
        ],
        "mlx": [
            {
                "id": e.id,
                "label": e.label,
                "size_gb": e.size_gb,
                "note": e.note,
                "downloaded": mlx_engines.downloaded(e),
                "problem": mlx_engines.problem(e.id),
            }
            for e in mlx_engines.MLX_ENGINES.values()
        ],
    }


# --- библиотека статей ---------------------------------------------------


def _check_score(path: Path) -> int | None:
    """Процент совпадения из check.md статьи; None — проверки не было."""
    report = path / CHECK
    if not report.is_file():
        return None
    m = _OVERALL.search(report.read_text(encoding="utf-8"))
    return int(m[1]) if m else None


def _state():
    from narrator import telegram

    try:
        return telegram.State.load(telegram.state_path())
    except (OSError, ValueError):
        return telegram.State()


def _unsent_ids(root: Path) -> set[str]:
    from narrator import telegram

    return {library.article_id(p) for p, _, _ in telegram.unsent(_state(), root)}


def _entry_view(path: Path, entry: Entry, unsent: set[str]) -> dict:
    engine = entry.engine or (sorted(entry.versions) or [None])[0]
    audio = path / library.audio_name(path, engine) if engine else None
    script_file = path / SCRIPT
    stale = script_file.is_file() and (
        audio is None or not audio.is_file() or audio.stat().st_mtime < script_file.stat().st_mtime
    )
    article_id = library.article_id(path)
    has_audio = bool(audio and audio.is_file())
    return {
        "id": article_id,
        "kind": entry.kind,
        "title": entry.title,
        "added": entry.added,
        "summary": entry.summary,
        "url": entry.url,
        "source_quality": entry.source_quality,
        "license": entry.license,  # право на переработку: без него в канал — только аннотация
        "engine": engine,
        "voice": entry.voice,
        "duration_sec": entry.duration_sec,
        "versions": entry.versions,
        "has_script": script_file.is_file(),
        "has_source": (path / SOURCE).is_file(),
        "has_audio": has_audio,
        "stale": stale,
        "check": _check_score(path),
        "sent": has_audio and article_id not in unsent,
    }


def library_view() -> dict:
    root = library.library_root()
    found = library.entries(root) if root.exists() else []
    unsent = _unsent_ids(root) if found else set()
    articles = [_entry_view(p, e, unsent) for p, e in found if e.kind == "article"]
    digests = [
        {"id": library.article_id(p), "title": e.title, "added": e.added}
        for p, e in found
        if e.kind == "digest" and (p / "narration.md").is_file()
    ]
    return {
        "root": str(root),
        "articles": articles,
        "digests": digests,
        "unsent": len([a for a in articles if a["has_audio"] and not a["sent"]]),
    }


def find_entry(article_id: str) -> tuple[Path, Entry]:
    root = library.library_root()
    for path, entry in library.entries(root) if root.exists() else []:
        if library.article_id(path) == article_id:
            return path, entry
    raise BridgeError(f"Статьи {article_id} нет в библиотеке.", kind="not_found")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def article(article_id: str) -> dict:
    path, entry = find_entry(article_id)
    view = _entry_view(path, entry, _unsent_ids(library.library_root()))
    audio = {
        engine: str(path / library.audio_name(path, engine))
        for engine in entry.versions
        if (path / library.audio_name(path, engine)).is_file()
    }
    text = path / ("narration.md" if entry.kind == "digest" else SCRIPT)
    return view | {
        "folder": str(path),
        "script": _read(text),
        "source": _read(path / SOURCE),
        "check_report": _read(path / CHECK),
        "audio": audio,
    }


def article_save(article_id: str, data: dict) -> dict:
    path, entry = find_entry(article_id)
    if "title" in data:
        title = str(data["title"] or "").strip()
        if not title:
            raise BridgeError("Название не может быть пустым.")
        entry.title = title
    if "summary" in data:
        entry.summary = str(data["summary"] or "").strip()
    if "license" in data:
        if data["license"] not in library.LICENSES:
            raise BridgeError(f"license — одно из: {', '.join(library.LICENSES)}.")
        entry.license = data["license"]
    if "script" in data:
        (path / SCRIPT).write_text(str(data["script"] or "").strip() + "\n", encoding="utf-8")
    entry.save(path / META_NAME)
    library.write_index(library.library_root())
    return article(article_id)


def article_new(data: dict) -> dict:
    """Статья из присланного текста: source.md и script.txt — этот текст как есть."""
    title = str(data.get("title") or "").strip()
    text = str(data.get("text") or "").strip()
    if not title or not text:
        raise BridgeError("Нужны название и текст статьи.")
    entry = Entry(
        title=title,
        added=library.today(),
        url=str(data.get("url") or "").strip() or None,
        summary=str(data.get("summary") or "").strip(),
        source_quality="pasted",
    )
    root = library.library_root()
    path = library.create_entry(root, entry)
    (path / SOURCE).write_text(text + "\n", encoding="utf-8")
    (path / SCRIPT).write_text(text + "\n", encoding="utf-8")
    library.write_index(root)
    return article(library.article_id(path))


def fetch(url: str) -> dict:
    """Скачать статью: каталог с source.md; script.txt пишет агент или человек."""
    from narrator.fetch import FetchError
    from narrator.fetch import fetch as download

    try:
        found = download(url)
    except FetchError as e:
        raise BridgeError(str(e)) from e
    entry = Entry(
        title=found.title or url,
        added=library.today(),
        url=url,
        author=found.author,
        published=found.published,
        source_quality="verbatim",
    )
    root = library.library_root()
    path = library.create_entry(root, entry)
    (path / SOURCE).write_text(found.to_markdown(), encoding="utf-8")
    library.write_index(root)
    return article(library.article_id(path))


# --- новости -------------------------------------------------------------


def _day(value: str | None) -> date:
    from narrator.news.cli import parse_day

    return parse_day(value) or date.today()


def sources_view() -> dict:
    from narrator.news import cli as news

    srcs = news.load_sources()
    with news.open_store() as store:
        stats = store.source_stats()
        failed = store.failed()
    out = []
    for s in srcs:
        st = stats.get(s.id, {})
        out.append(
            {
                "id": s.id,
                "name": s.name,
                "kind": s.kind,
                "url": s.url,
                "authority": s.authority,
                "enabled": s.enabled,
                "secondary": s.secondary,
                "note": s.note,
                "last": st.get("last"),
                "total": st.get("total", 0),
                "processed": st.get("processed", 0),
                "important": st.get("important", 0),
                "issued": st.get("issued", 0),
                "useless": s.enabled and news.useless(st),
                "failed": s.enabled and s.id in failed,
            }
        )
    return {"sources": out, "useless_after": news.USELESS_AFTER}


def failed_view() -> dict:
    from narrator.news import cli as news

    srcs = [s for s in news.load_sources() if s.enabled]
    with news.open_store() as store:
        failed = store.failed()
    return {
        "failed": [
            {
                "source": s.id,
                "name": s.name,
                "kind": s.kind,
                "url": s.url,
                "feed": s.feed,
                "at": failed[s.id]["at"],
                "error": failed[s.id]["error"],
            }
            for s in srcs
            if s.id in failed
        ]
    }


def top(day_value: str | None, limit: int) -> dict:
    from narrator.news import cli as news
    from narrator.news import digest, sources

    srcs = sources.by_id(news.load_sources())
    day = _day(day_value)
    with news.open_store() as store:
        events = digest.group_events(news.issue_rows(store, 30, None, day=day))
    out = []
    for ev in events[:limit]:
        lead = ev.lead
        names = sorted({getattr(srcs.get(i["source"]), "name", i["source"]) for i in ev.items})
        out.append(
            {
                "key": ev.key,
                "importance": ev.importance,
                "score": round(ev.score, 3),
                "title": lead["title_ru"],
                "kind": lead["kind"],
                "url": lead["url"],
                "why": lead.get("why") or "",
                "sources": names,
                "issued": any(i.get("digest") for i in ev.items),
            }
        )
    return {"day": day.isoformat(), "events": out}


def issue(day_value: str | None) -> dict:
    """Выпуск дня: есть ли он, где лежит, что сказал судья."""
    from narrator.news import digest
    from narrator.news.cli import DIGEST_SLUG, NARRATION

    day = _day(day_value)
    folder = library.day_dir(library.library_root(), day) / DIGEST_SLUG
    review = folder / digest.REVIEW
    text = _read(review)
    return {
        "day": day.isoformat(),
        "folder": str(folder),
        "exists": (folder / NARRATION).is_file(),
        "reviewed": review.is_file(),
        "verdict": digest.review_verdict(text) if text else None,
        "review": text,
    }


def reviews() -> dict:
    root = library.library_root()
    found = library.entries(root) if root.exists() else []
    return {
        "reviews": [
            {
                "id": library.article_id(p),
                "kind": e.kind,
                "title": e.title,
                "added": e.added,
                "folder": str(p),
                "files": [name for name in REVIEW_FILES if (p / name).is_file()],
            }
            for p, e in found
            if e.kind in ("weekly", "monthly")
        ]
    }


def review(review_id: str) -> dict:
    path, entry = find_entry(review_id)
    if entry.kind not in ("weekly", "monthly"):
        raise BridgeError(f"{review_id} — не обзор недели или месяца.")
    return {
        "id": review_id,
        "kind": entry.kind,
        "title": entry.title,
        "folder": str(path),
        "files": {name: _read(path / name) for name in REVIEW_FILES if (path / name).is_file()},
    }


# --- Telegram ------------------------------------------------------------


def bot(check: bool) -> dict:
    from narrator import telegram

    out: dict = {"token": False, "token_error": None, "username": None, "channels": {}}
    try:
        token = telegram.read_token()
        out["token"] = True
    except telegram.TelegramError as e:
        token, out["token_error"] = None, str(e)
    try:
        out["channels"] = telegram.channels()
    except telegram.TelegramError as e:
        out["channels_error"] = str(e)
    out["channels_file"] = str(telegram.channels_path())
    if check and token:
        try:
            me = telegram.Bot(token).call("getMe") or {}
            out["username"] = me.get("username")
        except telegram.TelegramError as e:
            out["token_error"] = str(e)
    return out


def subscribers(sync: bool) -> dict:
    from narrator import telegram

    path = telegram.state_path()
    state = _state()
    new: list[int] = []
    error = None
    if sync:
        try:
            for sub in telegram.pull(telegram.Bot(telegram.read_token()), state):
                new.append(sub.chat_id)
            state.save(path)
        except (telegram.TelegramError, OSError) as e:
            error = str(e)
    return {
        "subscribers": [asdict(s) for s in state.subscribers],
        "active": len(state.active()),
        "new": new,
        "error": error,
        "file": str(path),
    }


def unsent() -> dict:
    from narrator import telegram

    root = library.library_root()
    todo = telegram.unsent(_state(), root) if root.exists() else []
    return {
        "unsent": [
            {"id": library.article_id(p), "title": e.title, "engine": engine}
            for p, e, engine in todo
        ]
    }


# --- разбор команды ------------------------------------------------------


def _stdin_json() -> dict:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as e:
        raise BridgeError(f"на входе не JSON: {e}") from e
    if not isinstance(data, dict):
        raise BridgeError("на входе ждём JSON-объект")
    return data


def run(args) -> dict:
    match args.bridge_cmd:
        case "version":
            return {"bridge_api": BRIDGE_API}
        case "engines":
            return engines()
        case "voices":
            return voices()
        case "models":
            return models_view()
        case "preview":
            return preview(sys.stdin.read())
        case "split":
            return split(sys.stdin.read())
        case "library":
            return library_view()
        case "article":
            return article(args.id)
        case "article-save":
            return article_save(args.id, _stdin_json())
        case "article-new":
            return article_new(_stdin_json())
        case "fetch":
            return fetch(args.url)
        case "sources":
            return sources_view()
        case "failed":
            return failed_view()
        case "top":
            return top(args.date, args.limit)
        case "issue":
            return issue(args.date)
        case "reviews":
            return reviews()
        case "review":
            return review(args.id)
        case "bot":
            return bot(args.check)
        case "subscribers":
            return subscribers(args.sync)
        case "unsent":
            return unsent()
    raise BridgeError(f"нет команды {args.bridge_cmd}")


def cmd_bridge(args) -> int:
    try:
        payload, code = run(args), 0
    except BridgeError as e:
        payload, code = {"error": str(e), "kind": e.kind}, 1
    except SystemExit as e:  # команды news падают через fail() → SystemExit с текстом в stderr
        payload = {
            "error": "команда движка завершилась с ошибкой — подробности в логе",
            "kind": "unavailable",
        }
        code = 1
        if isinstance(e.code, str):
            payload["error"] = e.code
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    return code


def add_parser(sub) -> None:
    s = sub.add_parser("bridge", help="данные для страниц платформы: JSON в stdout")
    b = s.add_subparsers(dest="bridge_cmd", required=True)
    for name, help_ in (
        ("version", "версия контракта моста: bridge_api"),
        ("engines", "движки: голоса, параметры, почему не запустится"),
        ("voices", "образцы голосов-клонов"),
        ("models", "модели озвучки на диске"),
        ("preview", "как прочитает модель текст со stdin"),
        ("split", "статьи markdown-файла со stdin"),
        ("library", "статьи библиотеки со статусами"),
        ("article-new", "статья из JSON со stdin: title, text, url, summary"),
        ("sources", "источники новостей: журнал сбора и польза"),
        ("failed", "источники со сбоем последнего сбора"),
        ("reviews", "обзоры недели и месяца"),
        ("unsent", "озвучки, что не ушли в Telegram"),
    ):
        b.add_parser(name, help=help_)
    for name, help_ in (
        ("article", "карточка статьи: текст, отчёт проверки, аудио"),
        ("article-save", "сохранить title, summary, script из JSON со stdin"),
        ("review", "файлы обзора"),
    ):
        b.add_parser(name, help=help_).add_argument("id")
    b.add_parser("fetch", help="скачать статью в библиотеку").add_argument("url")
    n = b.add_parser("top", help="события дня по важности")
    n.add_argument("--date")
    n.add_argument("--limit", type=int, default=30)
    b.add_parser("issue", help="выпуск дня и вердикт судьи").add_argument("--date")
    b.add_parser("bot", help="токен, имя бота, каналы").add_argument("--check", action="store_true")
    b.add_parser("subscribers", help="подписчики бота").add_argument("--sync", action="store_true")
    s.set_defaults(func=cmd_bridge)
