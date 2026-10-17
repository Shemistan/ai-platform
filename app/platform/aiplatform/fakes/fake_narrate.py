"""Имитация движка narrator (app/narrator) для демо-режима и тестов.

Те же команды и флаги, что у `narrate`, те же строки вывода (вне терминала) и
те же JSON-ответы моста `narrate bridge …`. Данные демо — в каталоге
$FAKE_NARRATOR_HOME (state.json, статьи, образцы голосов); при первом вызове
он заполняется примерами: статьи в разных состояниях, подписчики, источники
со сбоем и без пользы, обзор недели.

`say` пишет mp3 длиной по числу слов (~150 слов в минуту): тон 220 Гц через
ffmpeg, без ffmpeg — WAV-заглушка. Окружение: FAKE_TTS_STEP_DELAY — пауза на
абзац, с (по умолчанию 0.3); FAKE_TTS_WORDS_PER_SECOND — темп речи (по умолчанию
2.5; тесты ускоряют: короче аудио — короче рендер); FAKE_TTS_FAIL=1 — упасть на середине;
FAKE_NARRATOR_DELAY — пауза шагов остальных долгих команд (по умолчанию 0.05).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import struct
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

WORDS_PER_SECOND = 2.5
MIN_SECONDS = 1.0
HOME_ENV = "FAKE_NARRATOR_HOME"
BRIDGE_API = 1  # как narrator.bridge.BRIDGE_API; FAKE_BRIDGE_API — подменить в тестах
ENGINES = (
    ("espeech", "ESpeech", "лучшее качество, нужна видеокарта", True, (32, 1.0)),
    ("silero", "Silero", "секунды на CPU, свои ударения", False, (0, 1.0)),
    ("supertonic", "Supertonic", "быстро на CPU", True, (8, 1.05)),
    ("higgs", "Higgs Audio v3", "клон голоса на Apple Silicon, около 6 ГБ", False, (0, 1.0)),
    ("fish", "Fish S1 mini", "клон голоса на Apple Silicon, около 2 ГБ", False, (0, 1.0)),
    ("moss", "MOSS-TTSD", "клон голоса на Apple Silicon, около 4 ГБ", False, (0, 1.0)),
    ("qwen3", "Qwen3-TTS", "клон голоса на Apple Silicon, около 4 ГБ", False, (0, 1.0)),
    ("chatterbox", "Chatterbox", "клон голоса на Apple Silicon, около 3 ГБ", False, (0, 1.0)),
)
CLONES = {"M1": "m1", "Charon": "charon", "Shem": "shem", "Shem-v2": "shem-v2"}
VOICES = {
    "silero": ["eugene", "aidar", "baya", "kseniya", "xenia"],
    "supertonic": [f"{g}{n}" for g in "FM" for n in range(1, 6)],
}
DEFAULT_VOICES = {"silero": "eugene", "supertonic": "M1"}
MLX = ("higgs", "fish", "moss", "qwen3", "chatterbox")
MLX_SIZES = {"higgs": 6, "fish": 2, "moss": 4, "qwen3": 4, "chatterbox": 3}
MAIN_MODELS = (
    ("espeech", "ESpeech-TTS-1"),
    ("vocos", "Вокодер vocos для ESpeech"),
    ("ruaccent", "Ударения RUAccent"),
    ("supertonic", "Supertonic 3"),
    ("silero", "Silero v5.5"),
    ("whisper", "Проверка распознаванием: faster-whisper small"),
)
REF_TEXT = "Я рассказываю о новостях разработки спокойно и ясно, чтобы было интересно слушать."
DIGITS = ["ноль", "один", "два", "три", "четыре", "пять", "шесть", "семь", "восемь", "девять"]
LATIN = {"go": "го", "github": "гитхаб", "api": "эй пи ай", "docker": "докер", "kafka": "кафка"}
TOKEN = "123456:demo"


# --- общее ---------------------------------------------------------------


def delay() -> None:
    time.sleep(float(os.environ.get("FAKE_NARRATOR_DELAY", "0.05")))


def emit(payload: dict, code: int = 0) -> int:
    print(json.dumps(payload, ensure_ascii=False))
    return code


def fail(message: str, code: int = 1) -> int:
    print(f"narrator: {message}", file=sys.stderr)
    return code


def paragraphs(text: str) -> list[str]:
    out, buf = [], []
    for line in text.splitlines() + [""]:
        if line.strip():
            buf.append(line.strip())
        elif buf:
            out.append(" ".join(buf))
            buf = []
    return out


def seconds_for(text: str) -> float:
    pace = float(os.environ.get("FAKE_TTS_WORDS_PER_SECOND") or WORDS_PER_SECOND)
    return max(len(text.split()) / pace, MIN_SECONDS)


def write_wav(path: Path, seconds: float, rate: int = 8000) -> None:
    frames = int(seconds * rate)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data))
    path.write_bytes(header + data)


def write_mp3(path: Path, seconds: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        write_wav(path, seconds)
        return
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds:.2f}"]
    cmd += ["-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", str(path)]
    subprocess.run(cmd, check=True)


def speak(blocks: list[str], out: Path, title: str, speed: float | None = None) -> float:
    """Строки прогресса, как у synth вне терминала, и mp3 по длине текста."""
    step = float(os.environ.get("FAKE_TTS_STEP_DELAY", "0.3"))
    total = 0.0
    for n, block in enumerate(blocks, start=1):
        print(f"{title} абзац {n}/{len(blocks)}", file=sys.stderr, flush=True)
        if os.environ.get("FAKE_TTS_FAIL") and n == max(len(blocks) // 2, 1):
            print("RuntimeError: fake failure requested by FAKE_TTS_FAIL", file=sys.stderr, flush=True)
            raise SystemExit(1)
        time.sleep(step)
        seconds = seconds_for(block) / (speed or 1.0)
        total += seconds
        print(f"[{n}/{len(blocks)}] {seconds:5.1f} с аудио за {step:4.0f} с  {block[:50]}", file=sys.stderr, flush=True)
    print(f"синтез: {step * len(blocks):.0f} с, склеиваю {len(blocks)} клипов", file=sys.stderr, flush=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_mp3(out, total)
    return total


def normalize(text: str) -> str:
    """Грубая замена нормализатора: цифры по одной словами, известная латиница — кириллицей."""

    def digits(m: re.Match) -> str:
        return " ".join(DIGITS[int(d)] for d in m[0])

    def latin(m: re.Match) -> str:
        return LATIN.get(m[0].lower(), m[0])

    return re.sub(r"[A-Za-z]+", latin, re.sub(r"\d+", digits, text))


def remarks(spoken: str) -> list[dict]:
    out = []
    for n, line in enumerate(spoken.splitlines(), start=1):
        if len(line.split()) > 40:
            out.append({"line": n, "col": 1, "level": "warning", "code": "LONG", "message": "длинное предложение"})
        if re.search(r"[A-Za-z]", line):
            out.append({"line": n, "col": 1, "level": "warning", "code": "LATIN", "message": "латиница"})
    return out


# --- данные демо ---------------------------------------------------------


def home() -> Path:
    path = Path(os.environ.get(HOME_ENV) or Path.cwd() / "fake-narrator")
    path.mkdir(parents=True, exist_ok=True)
    return path


def state_file() -> Path:
    return home() / "state.json"


def load() -> dict:
    path = state_file()
    if not path.is_file():
        # Демо заполняется один раз: другой процесс мог успеть первым — его данные главнее.
        tmp = path.with_name(f"seed-{os.getpid()}.tmp")
        tmp.write_text(json.dumps(seed(), ensure_ascii=False, indent=1), encoding="utf-8")
        try:
            os.link(tmp, path)
        except FileExistsError:
            pass
        finally:
            tmp.unlink()
    return json.loads(path.read_text(encoding="utf-8"))


def save(state: dict) -> None:
    tmp = state_file().with_name(f"state-{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(state_file())


def seed() -> dict:
    today = date.today()
    day = lambda n: (today - timedelta(days=n)).isoformat()  # noqa: E731
    articles = {
        f"{day(5)}-queues": {
            "title": "Очереди без боли",
            "added": day(5),
            "summary": "Почему брокер сообщений — не база данных и как это учитывать.",
            "url": "https://example.dev/queues",
            "source_quality": "verbatim",
            "script": "Брокер сообщений — не база данных.\n\nСообщения приходят повторно, и это нормально.\n",
            "source": "# Очереди без боли\n\nБрокер сообщений — не база данных.\n",
            "voiced": "silero",
            "check": 96,
            "sent": True,
        },
        f"{day(2)}-go-iterators": {
            "title": "Итераторы в Go 1.23",
            "added": day(2),
            "summary": "Как устроены итераторы по функциям и где они упрощают код.",
            "url": "https://example.dev/iterators",
            "source_quality": "verbatim",
            "script": "В Go 1.23 появились итераторы.\n\nОни упрощают обход коллекций.\n",
            "source": "# Итераторы\n\nВ Go 1.23 появились итераторы.\n",
            "voiced": "espeech",
            "check": 81,
            "sent": False,
        },
        f"{day(0)}-postgres-locks": {
            "title": "Блокировки в PostgreSQL",
            "added": day(0),
            "summary": "",
            "url": "https://example.dev/locks",
            "source_quality": "verbatim",
            "script": "",
            "source": "# Блокировки в PostgreSQL\n\nКакие блокировки берёт ALTER TABLE.\n\nКак их избежать.\n",
            "voiced": None,
            "check": None,
            "sent": False,
        },
    }
    sources = [
        ("go_blog", "Go Blog", "official", 0.95, True, None, 40, 6, 5),
        ("github_changelog", "GitHub Changelog", "official", 0.9, True, None, 120, 9, 7),
        ("postgres_news", "PostgreSQL News", "official", 0.9, True, "HTTP 429 Too Many Requests", 25, 4, 3),
        ("uber_engineering", "Uber Engineering", "blog", 0.8, True, "HTTP 404 Not Found", 12, 1, 1),
        ("hacker_news", "Hacker News", "community", 0.5, True, None, 300, 7, 4),
        ("random_newsletter", "Random Newsletter", "newsletter", 0.4, True, None, 22, 0, 0),
        ("habr_go", "Хабр: Go", "community", 0.5, True, None, 60, 2, 1),
        ("old_blog", "Старый блог", "blog", 0.3, False, None, 0, 0, 0),
    ]
    return {
        "articles": articles,
        "subscribers": [
            {"chat_id": 1001, "name": "Аня", "added": day(30), "active": True, "note": "", "thread": None},
            {"chat_id": 1002, "name": "Борис", "added": day(12), "active": True, "note": "", "thread": None},
            {"chat_id": 1003, "name": "Вера", "added": day(9), "active": False, "note": "/stop", "thread": None},
        ],
        "sources": [
            {
                "id": sid,
                "name": name,
                "kind": kind,
                "url": f"https://example.dev/{sid}",
                "feed": f"https://example.dev/{sid}/feed.xml",
                "authority": weight,
                "enabled": enabled,
                "secondary": kind in ("community", "newsletter"),
                "note": "" if enabled else "выключен: лента пропала",
                "error": error,
                "total": total,
                "processed": total,
                "important": important,
                "issued": issued,
                "at": f"{day(0)}T07:00:00+03:00",
                "new": 3 if not error else 0,
            }
            for sid, name, kind, weight, enabled, error, total, important, issued in sources
        ],
        "reviews": {
            f"youtube_news-week_{day(13)}_{day(7)}": {
                "kind": "weekly",
                "title": "Главное в разработке за неделю",
                "added": day(7),
                "files": {
                    "teleprompter.md": "# Неделя\n\nПривет! Это главное за неделю.\n\nНачнём с релиза Go.\n",
                    "materials.md": "- [M1] скриншот заметок к релизу Go\n",
                    "post.md": "**Главное за неделю**\n\n🐹 Релиз Go — [заметки](https://go.dev)\n",
                    "events.md": "# События недели\n\n- 8/10 Релиз Go\n",
                },
            }
        },
        "models": {"espeech": True, "vocos": True, "ruaccent": True, "supertonic": True, "whisper": True},
        "mlx": {"higgs": True},
        "custom_voices": {},
        "offset": 0,
    }


def article_dir(article_id: str) -> Path:
    return home() / "library" / article_id


def audio_path(article_id: str, engine: str) -> Path:
    return article_dir(article_id) / f"{article_id}_{engine}.mp3"


def ensure_audio(state: dict) -> None:
    """mp3 озвученных статей демо: создаются при первом обращении."""
    for article_id, a in state["articles"].items():
        if a.get("voiced"):
            path = audio_path(article_id, a["voiced"])
            if not path.is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                write_mp3(path, seconds_for(a["script"]))


def article_view(article_id: str, a: dict) -> dict:
    voiced = a.get("voiced")
    return {
        "id": article_id,
        "kind": "article",
        "title": a["title"],
        "added": a["added"],
        "summary": a["summary"],
        "url": a.get("url"),
        "source_quality": a.get("source_quality", "verbatim"),
        "license": a.get("license", "none"),
        "engine": voiced,
        "voice": "Charon" if voiced else None,
        "duration_sec": round(seconds_for(a["script"]), 1) if voiced else None,
        "versions": {voiced: round(seconds_for(a["script"]), 1)} if voiced else {},
        "has_script": bool(a["script"].strip()),
        "has_source": bool(a.get("source")),
        "has_audio": bool(voiced),
        "stale": bool(a["script"].strip()) and (not voiced or a.get("stale", False)),
        "check": a.get("check"),
        "sent": bool(voiced) and a.get("sent", False),
    }


def find(state: dict, article_id: str) -> dict:
    if article_id not in state["articles"]:
        raise LookupError(f"Статьи {article_id} нет в библиотеке.")
    return state["articles"][article_id]


def check_report(score: int | None) -> str:
    if score is None:
        return ""
    return f"# Проверка озвучки\n\nСовпадение по словам: {score}%, блоков ниже 85%: 1 из 4.\n"


def article_full(article_id: str, a: dict) -> dict:
    voiced = a.get("voiced")
    return article_view(article_id, a) | {
        "folder": str(article_dir(article_id)),
        "script": a["script"],
        "source": a.get("source", ""),
        "check_report": check_report(a.get("check")),
        "audio": {voiced: str(audio_path(article_id, voiced))} if voiced else {},
    }


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "article"


def voice_sample(stem: str) -> Path:
    path = home() / "voices" / f"{stem}.wav"
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_wav(path, 6.0)
    return path


# --- мост ----------------------------------------------------------------


def bridge(args, state: dict) -> dict:
    ensure_audio(state)
    match args.bridge_cmd:
        case "version":
            return {"bridge_api": int(os.environ.get("FAKE_BRIDGE_API", BRIDGE_API))}
        case "engines":
            ready = set(state["mlx"])
            out = []
            for eid, label, note, tunable, (steps, speed) in ENGINES:
                problem = None
                if eid in MLX and eid not in ready:
                    problem = f"модель не скачана: ./narrate setup --mlx {eid}"
                if eid == "silero" and not state["models"].get("silero"):
                    problem = "нет модели Silero"
                voices = VOICES.get(eid, list(CLONES))
                out.append(
                    {
                        "id": eid,
                        "label": label,
                        "note": note,
                        "voices": voices,
                        "default_voice": DEFAULT_VOICES.get(eid, "Charon"),
                        "default_steps": steps or None,
                        "default_speed": speed,
                        "tunable": tunable,
                        "problem": problem,
                    }
                )
            return {
                "engines": out,
                "verify_available": bool(state["models"].get("whisper")),
                "models_dir": str(home() / "models"),
                "models_dir_exists": True,
                "models_hint": "скачай один раз вне sandbox: make models",
                "steps_range": [1, 128],
                "speed_range": [0.7, 2.0],
            }
        case "voices":
            out = []
            for voice, stem in CLONES.items():
                custom = state["custom_voices"].get(stem)
                out.append(
                    {
                        "voice": voice,
                        "stem": stem,
                        "record": None if voice == "M1" else ("gemini" if voice == "Charon" else "file"),
                        "sample": str(voice_sample(stem)),
                        "seconds": 6.0,
                        "text": (custom or {}).get("text") or REF_TEXT,
                        "custom": bool(custom),
                    }
                )
            return {
                "voices": out,
                "seconds_range": [3.0, 12.0],
                "default_text": REF_TEXT,
                "voices_dir": str(home() / "voices"),
            }
        case "models":
            return {
                "root": str(home() / "models"),
                "root_exists": True,
                "hint": "скачай один раз вне sandbox: make models",
                "main": [
                    {
                        "id": mid,
                        "label": label,
                        "path": str(home() / "models" / mid),
                        "exists": bool(state["models"].get(mid)),
                    }
                    for mid, label in MAIN_MODELS
                ],
                "mlx": [
                    {
                        "id": eid,
                        "label": label,
                        "size_gb": MLX_SIZES[eid],
                        "note": note.split(", около")[0],
                        "downloaded": eid in state["mlx"],
                        "problem": None if eid in state["mlx"] else f"модель не скачана: ./narrate setup --mlx {eid}",
                    }
                    for eid, label, note, _, _ in ENGINES
                    if eid in MLX
                ],
            }
        case "preview":
            text = sys.stdin.read()
            if not text.strip():
                return {"script": "", "changed": 0, "issues": []}
            spoken = normalize(text)
            changed = sum(a != b for a, b in zip(text.splitlines(), spoken.splitlines(), strict=False))
            return {"script": spoken, "changed": changed, "issues": remarks(text)}
        case "split":
            text = sys.stdin.read()
            parts = re.split(r"\n(?=# )", "\n" + text)
            parts = [p.strip() + "\n" for p in parts if p.strip()]
            if len(parts) < 2:
                parts = [p.strip() + "\n" for p in re.split(r"\n\s*---\s*\n", text) if p.strip()]
            if len(parts) > 100:
                raise ValueError(f"В файле {len(parts)} статей, за раз — не больше 100.")
            return {
                "articles": [
                    {"title": p.splitlines()[0].lstrip("#").strip()[:90], "words": len(p.split()), "text": p}
                    for p in parts
                ]
            }
        case "library":
            articles = sorted(
                (article_view(i, a) for i, a in state["articles"].items()),
                key=lambda a: (a["added"], a["id"]),
                reverse=True,
            )
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            return {
                "root": str(home() / "library"),
                "articles": articles,
                "digests": [{"id": f"{yesterday}-digest", "title": "Дайджест разработчика", "added": yesterday}],
                "unsent": sum(a["has_audio"] and not a["sent"] for a in articles),
            }
        case "article":
            if args.id.endswith("-digest"):
                return {"id": args.id, "kind": "digest", "title": "Дайджест разработчика", "script": DIGEST_TEXT}
            return article_full(args.id, find(state, args.id))
        case "article-save":
            a = find(state, args.id)
            data = json.loads(sys.stdin.read() or "{}")
            if "title" in data:
                if not str(data["title"]).strip():
                    raise ValueError("Название не может быть пустым.")
                a["title"] = str(data["title"]).strip()
            if "summary" in data:
                a["summary"] = str(data["summary"] or "").strip()
            if "license" in data:
                if data["license"] not in ("own", "cc-by", "permission", "none"):
                    raise ValueError("license — одно из: own, cc-by, permission, none.")
                a["license"] = data["license"]
            if "script" in data:
                a["script"] = str(data["script"] or "").strip() + "\n"
                a["stale"] = True
            save(state)
            return article_full(args.id, a)
        case "article-new":
            data = json.loads(sys.stdin.read() or "{}")
            title, text = str(data.get("title") or "").strip(), str(data.get("text") or "").strip()
            if not title or not text:
                raise ValueError("Нужны название и текст статьи.")
            article_id = f"{date.today().isoformat()}-{slug(title)}"
            while article_id in state["articles"]:
                article_id += "-2"
            state["articles"][article_id] = {
                "title": title,
                "added": date.today().isoformat(),
                "summary": str(data.get("summary") or "").strip(),
                "url": data.get("url") or None,
                "source_quality": "pasted",
                "script": text + "\n",
                "source": text + "\n",
                "voiced": None,
                "check": None,
                "sent": False,
            }
            save(state)
            return article_full(article_id, state["articles"][article_id])
        case "fetch":
            if "fail" in args.url:
                raise ValueError(f"{args.url}: страница не скачалась (HTTP 403)")
            name = args.url.rstrip("/").rsplit("/", 1)[-1] or "article"
            article_id = f"{date.today().isoformat()}-{slug(name)}"
            title = name.replace("-", " ").capitalize()
            state["articles"][article_id] = {
                "title": title,
                "added": date.today().isoformat(),
                "summary": "",
                "url": args.url,
                "source_quality": "verbatim",
                "script": "",
                "source": f"# {title}\n\n- ссылка: {args.url}\n\nПервый абзац статьи.\n\nВторой абзац статьи.\n",
                "voiced": None,
                "check": None,
                "sent": False,
            }
            save(state)
            return article_full(article_id, state["articles"][article_id])
        case "sources":
            out = []
            for s in state["sources"]:
                last = None
                if s["total"] or s["error"]:
                    last = {
                        "source": s["id"],
                        "at": s["at"],
                        "ok": not s["error"],
                        "new": s["new"],
                        "error": s["error"],
                    }
                out.append(
                    {k: s[k] for k in ("id", "name", "kind", "url", "authority", "enabled", "secondary", "note")}
                    | {k: s[k] for k in ("total", "processed", "important", "issued")}
                    | {
                        "last": last,
                        "useless": s["enabled"] and s["processed"] >= 15 and not s["important"] and not s["issued"],
                        "failed": s["enabled"] and bool(s["error"]) and not s.get("rescued"),
                    }
                )
            return {"sources": out, "useless_after": 15}
        case "failed":
            return {
                "failed": [
                    {k: s[k] for k in ("name", "kind", "url", "feed", "at", "error")} | {"source": s["id"]}
                    for s in state["sources"]
                    if s["enabled"] and s["error"] and not s.get("rescued")
                ]
            }
        case "top":
            day = args.date or date.today().isoformat()
            return {"day": day, "events": [dict(e) for e in TOP_EVENTS][: args.limit]}
        case "issue":
            day = args.date or date.today().isoformat()
            return {"day": day, "folder": "", "exists": False, "reviewed": False, "verdict": None, "review": ""}
        case "reviews":
            return {
                "reviews": [
                    {
                        "id": rid,
                        "kind": r["kind"],
                        "title": r["title"],
                        "added": r["added"],
                        "folder": str(home() / "library" / rid),
                        "files": list(r["files"]),
                    }
                    for rid, r in sorted(state["reviews"].items(), key=lambda x: x[1]["added"], reverse=True)
                ]
            }
        case "review":
            r = state["reviews"].get(args.id)
            if r is None:
                raise LookupError(f"Статьи {args.id} нет в библиотеке.")
            return {"id": args.id, "kind": r["kind"], "title": r["title"], "folder": "", "files": r["files"]}
        case "bot":
            return {
                "token": True,
                "token_error": None,
                "username": "demo_narrator_bot" if args.check else None,
                "channels": {"HISTORY_CHAN": "-1001111111111", "VOICE_NEWS": "-1002222222222"},
                "channels_file": str(home() / "telegram_tems.md"),
            }
        case "subscribers":
            return {
                "subscribers": state["subscribers"],
                "active": sum(s["active"] for s in state["subscribers"]),
                "new": [],
                "error": None,
                "file": str(home() / "telegram.json"),
            }
        case "unsent":
            return {
                "unsent": [
                    {"id": i, "title": a["title"], "engine": a["voiced"]}
                    for i, a in state["articles"].items()
                    if a.get("voiced") and not a.get("sent")
                ]
            }
    raise ValueError(f"нет команды {args.bridge_cmd}")


DIGEST_TEXT = "Это дайджест разработчика за вчера.\n\nГлавное — новый релиз Go.\n"
TOP_EVENTS = (
    {
        "key": "go-1-26",
        "importance": 9,
        "score": 0.91,
        "title": "Вышел Go 1.26",
        "kind": "release",
        "url": "https://go.dev/blog",
        "why": "Новый сборщик мусора",
        "sources": ["Go Blog", "Hacker News"],
        "issued": True,
    },
    {
        "key": "pgbouncer-cve",
        "importance": 8,
        "score": 0.85,
        "title": "PgBouncer закрыл обход аутентификации",
        "kind": "security",
        "url": "https://example.dev/pgbouncer",
        "why": "Обновиться всем",
        "sources": ["PostgreSQL News"],
        "issued": True,
    },
    {
        "key": "copilot-model",
        "importance": 5,
        "score": 0.52,
        "title": "Новая модель в меню Copilot",
        "kind": "ai_tool",
        "url": "https://example.dev/copilot",
        "why": "",
        "sources": ["GitHub Changelog"],
        "issued": False,
    },
)


# --- долгие команды ------------------------------------------------------


def cmd_synth(args, state: dict) -> int:
    a = find(state, args.dir)
    if not a["script"].strip():
        return fail(f"{args.dir}/script.txt: нет текста для озвучки")
    if not a["summary"].strip():
        return fail(f"{args.dir}/meta.json: пустой summary — опиши статью для реестра")
    out = audio_path(args.dir, args.engine)
    seconds = speak(paragraphs(a["script"]), out, a["title"])
    a.update(voiced=args.engine, stale=False, sent=a.get("sent") and not args.no_send)
    print(f"{out}  {seconds:.0f} с")
    if not args.no_send:
        a["sent"] = True
        print("телеграм: озвучка отправлена 1 из 1")
    save(state)
    return 0


def cmd_verify(args, state: dict) -> int:
    a = find(state, args.dir)
    if not a.get("voiced"):
        return fail("сначала narrator synth")
    for n in range(1, 4):
        print(f"распознаю абзац {n}/3", flush=True)
        delay()
    a["check"] = 92
    save(state)
    print("Совпадение по словам: 92%, блоков ниже 85%: 1 из 3.")
    return 0


def cmd_render(args, state: dict) -> int:
    todo = [
        i
        for i, a in state["articles"].items()
        if a["script"].strip() and (args.all or not a.get("voiced") or a.get("stale"))
    ]
    if not todo:
        print("озвучивать нечего: у всех каталогов с script.txt аудио свежее")
        return 0
    print(f"к озвучке {len(todo)}: " + ", ".join(todo), flush=True)
    failed = []
    for k, article_id in enumerate(todo, start=1):
        print(f"\n=== {k}/{len(todo)} {article_id}", flush=True)
        args.dir = article_id
        if cmd_synth(args, state) != 0:
            failed.append(article_id)
            continue
        if not args.no_verify:
            cmd_verify(args, state)
    if failed:
        return fail("не озвучены: " + ", ".join(failed))
    return 0


def cmd_bot(args, state: dict) -> int:
    match args.bot_cmd:
        case "send":
            a = find(state, args.dir)
            if not a.get("voiced"):
                return fail(f"{args.dir}: статья ещё не озвучена")
            if a.get("sent") and not args.force and not args.channel:  # как narrator: без --force не повторяет
                print(f"телеграм: {args.dir} уже отправлена, повтор — --force")
                return 0
            delay()
            a["sent"] = True
            save(state)
            where = f" в {args.channel}" if args.channel else ""
            print(f"телеграм{where}: озвучка отправлена 1 из 1")
        case "unsent":
            todo = [i for i, a in state["articles"].items() if a.get("voiced") and not a.get("sent")]
            if not todo:
                print("телеграм: неотправленных озвучек нет")
                return 0
            print(f"не отправлено {len(todo)}:")
            for k, article_id in enumerate(todo, start=1):
                state["articles"][article_id]["sent"] = True
                delay()
                print(f"{k}/{len(todo)} {article_id}" if not args.mark else f"  {article_id}", flush=True)
            if args.mark:
                print(f"отмечено отправленными {len(todo)}, в телеграм ничего не ушло")
            save(state)
        case "subscribers":
            print(f"подписчиков {sum(s['active'] for s in state['subscribers'])}")
        case "serve":
            print("бот @demo_narrator_bot слушает подписки, выход — Ctrl+C", flush=True)
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
            n = 0
            while True:
                time.sleep(float(os.environ.get("FAKE_BOT_POLL", "1.0")))
                n += 1
                if n == 2:
                    fresh = load()
                    chat = 2000 + len(fresh["subscribers"])
                    fresh["subscribers"].append(
                        {
                            "chat_id": chat,
                            "name": "Новый читатель",
                            "added": date.today().isoformat(),
                            "active": True,
                            "note": "",
                            "thread": None,
                        }
                    )
                    save(fresh)
                    print(f"подписался: {chat} Новый читатель", flush=True)
                if args.once:
                    return 0
    return 0


def cmd_news(args, state: dict) -> int:
    srcs = [s for s in state["sources"] if s["enabled"] and (not args.only or s["id"] in args.only)]
    if args.only and not srcs:
        return fail(f"нет таких включённых источников: {sorted(args.only)}")
    for s in srcs:
        delay()
        s["at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        if s["error"]:
            print(f"  сбой  {s['id']:<26} {s['error']}", flush=True)
            continue
        s["new"] = 2
        s["total"] += 2
        print(f"  ок    {s['id']:<26} материалов   5, новых 2", flush=True)
    save(state)
    print(f"\nисточников {len(srcs)}, со сбоем {sum(bool(s['error']) for s in srcs)}, новых материалов {2 * len(srcs)}")
    return 0


def cmd_voice(args, state: dict) -> int:
    stems = {stem for voice, stem in CLONES.items() if voice != "M1"}
    stem = args.name.lower()
    if stem not in stems:
        return fail(f"голос {args.name!r} не записывается, есть: {', '.join(sorted(stems))}")
    if not args.source and stem != "charon":
        return fail(f"голоса {stem} в Gemini TTS нет: передай запись и её текст")
    if not args.source and not os.environ.get("GEMINI_API_KEY"):
        return fail("нет ключа Gemini: задай $GEMINI_API_KEY или передай файл записи", 2)
    target = home() / "voices" / f"{stem}.wav"
    target.parent.mkdir(parents=True, exist_ok=True)
    if args.source:
        shutil.copyfile(args.source, target)
    else:
        write_wav(target, 6.0)
    state["custom_voices"][stem] = {"text": (args.text or REF_TEXT).strip()}
    save(state)
    print(f"{target}  6.0 с")
    return 0


def cmd_make(args, state: dict) -> int:
    """`make -C app/narrator <цель> K=V` в демо: агенты и модели."""
    params = dict(p.split("=", 1) for p in args.params if "=" in p)
    match args.target:
        case "article-prepare":
            a = find(state, params["A"])
            print("агент читает source.md…", flush=True)
            delay()
            body = [
                line for line in a.get("source", "").splitlines() if line.strip() and not line.startswith(("#", "- "))
            ]
            a["script"] = "\n\n".join(body or ["Текст статьи."]) + "\n"
            a["summary"] = a["summary"] or f"Пересказ статьи «{a['title']}»."
            a["stale"] = True
            save(state)
            print("script.txt и карточка готовы, lint: ошибок 0", flush=True)
        case "news-rescue":
            for s in state["sources"]:
                if s["enabled"] and s["error"] and not s.get("rescued"):
                    delay()
                    s["rescued"] = True
                    print(f"  добор {s['id']:<26} материалов   3, новых 3", flush=True)
            save(state)
            print("новых материалов 3; без добора осталось 0")
        case "news-weekly" | "news-monthly":
            today = date.today()
            if args.target == "news-weekly":
                rid, kind, title = (
                    f"youtube_news-week_{(today - timedelta(days=6)).isoformat()}_{today.isoformat()}",
                    "weekly",
                    "Главное за неделю",
                )
            else:
                month = params.get("MONTH") or today.strftime("%Y-%m")
                rid, kind, title = f"youtube_news-month_{month}", "monthly", f"Главное за месяц {month}"
            for stage in ("сбор источников", "агент отбирает события", "агент пишет суфлёр"):
                print(stage, flush=True)
                delay()
            state["reviews"][rid] = {
                "kind": kind,
                "title": title,
                "added": today.isoformat(),
                "files": {
                    "teleprompter.md": f"# {title}\n\nПривет! Это {title.lower()}.\n",
                    "materials.md": "- [M1] заметки к релизу\n",
                    "post.md": f"**{title}**\n",
                    "events.md": "# События\n",
                },
            }
            save(state)
            print(rid)
        case "models":
            for mid, label in MAIN_MODELS:
                delay()
                state["models"][mid] = True
                print(f"== {label}: готово", flush=True)
            save(state)
        case "models-mlx":
            for eid in (params.get("E") or " ".join(MLX)).split():
                delay()
                state["mlx"][eid] = True
                print(f"== {eid}: модель скачана", flush=True)
            save(state)
        case "move-models":
            delay()
            print("переносить нечего: всё уже в папке моделей")
        case _:
            return fail(f"нет цели {args.target}")
    return 0


def cmd_say(args) -> int:
    text = Path(args.file).read_text(encoding="utf-8")
    blocks = paragraphs(text)
    if not blocks:
        print(f"{args.file}: текст для озвучки пуст", file=sys.stderr)
        return 2
    total = speak(blocks, Path(args.out), os.path.basename(args.file), args.speed)
    print(f"{args.out}  {total:.0f} с")
    if args.check_json and not args.no_verify:
        weak = [
            {
                "line": 1,
                "score": 0.71,
                "text": blocks[0],
                "heard": blocks[0].split()[0],
                "missed": blocks[0].split()[1:3],
            }
        ]
        Path(args.check_json).write_text(
            json.dumps({"overall": 0.93, "threshold": 0.85, "total": len(blocks), "weak": weak}, ensure_ascii=False),
            encoding="utf-8",
        )
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fake-narrate")
    sub = p.add_subparsers(dest="cmd", required=True)
    say = sub.add_parser("say")
    say.add_argument("file")
    say.add_argument("--engine", required=True)
    say.add_argument("--voice")
    say.add_argument("--speed", type=float)
    say.add_argument("--steps", type=int)
    say.add_argument("--out", required=True)
    say.add_argument("--no-verify", action="store_true")
    say.add_argument("--check-json")

    b = sub.add_parser("bridge").add_subparsers(dest="bridge_cmd", required=True)
    for name in (
        "version", "engines", "voices", "models", "preview", "split", "library", "article-new",
        "sources", "failed", "reviews", "unsent",
    ):  # fmt: skip
        b.add_parser(name)
    for name in ("article", "article-save", "review"):
        b.add_parser(name).add_argument("id")
    b.add_parser("fetch").add_argument("url")
    top = b.add_parser("top")
    top.add_argument("--date")
    top.add_argument("--limit", type=int, default=30)
    b.add_parser("issue").add_argument("--date")
    b.add_parser("bot").add_argument("--check", action="store_true")
    b.add_parser("subscribers").add_argument("--sync", action="store_true")

    for name in ("synth", "verify"):
        s = sub.add_parser(name)
        s.add_argument("dir")
        s.add_argument("--engine", default="espeech")
        s.add_argument("--voice")
        s.add_argument("--speed", type=float)
        s.add_argument("--steps", type=int)
        s.add_argument("--no-send", action="store_true")
    r = sub.add_parser("render")
    r.add_argument("--engine", default="espeech")
    r.add_argument("--voice")
    r.add_argument("--speed", type=float)
    r.add_argument("--steps", type=int)
    r.add_argument("--all", action="store_true")
    r.add_argument("--no-verify", action="store_true")
    r.add_argument("--no-send", action="store_true")

    bot = sub.add_parser("bot").add_subparsers(dest="bot_cmd", required=True)
    send = bot.add_parser("send")
    send.add_argument("dir")
    send.add_argument("--channel")
    send.add_argument("--force", action="store_true")
    unsent = bot.add_parser("unsent")
    unsent.add_argument("--mark", action="store_true")
    unsent.add_argument("--list", action="store_true")
    bot.add_parser("subscribers").add_argument("--no-sync", action="store_true")
    serve = bot.add_parser("serve")
    serve.add_argument("--once", action="store_true")
    serve.add_argument("--updates", action="store_true")

    news = sub.add_parser("news").add_subparsers(dest="news_cmd", required=True)
    collect = news.add_parser("collect")
    collect.add_argument("--only", action="append")
    collect.add_argument("--date")

    voice = sub.add_parser("voice")
    voice.add_argument("name")
    voice.add_argument("--from", dest="source")
    voice.add_argument("--text")

    make = sub.add_parser("make")
    make.add_argument("target")
    make.add_argument("params", nargs="*")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(sys.argv[1:] if argv is None else argv)
    if args.cmd == "say":
        return cmd_say(args)
    state = load()
    if args.cmd == "bridge":
        try:
            return emit(bridge(args, state))
        except LookupError as exc:  # как BridgeError(kind="not_found") в движке
            return emit({"error": str(exc).strip("'\""), "kind": "not_found"}, 1)
        except ValueError as exc:
            return emit({"error": str(exc).strip("'\""), "kind": "invalid"}, 1)
    try:
        match args.cmd:
            case "synth":
                return cmd_synth(args, state)
            case "verify":
                return cmd_verify(args, state)
            case "render":
                return cmd_render(args, state)
            case "bot":
                return cmd_bot(args, state)
            case "news":
                return cmd_news(args, state)
            case "voice":
                return cmd_voice(args, state)
            case "make":
                return cmd_make(args, state)
    except LookupError as exc:
        return fail(str(exc).strip("'\""))
    return fail(f"нет команды {args.cmd}")


if __name__ == "__main__":
    sys.exit(main())
