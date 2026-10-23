"""Задачи «Расшифровки»: видео или аудио → текст. Хранение на диске, очередь из одного воркера.

звук (ffmpeg, 16 кГц моно) → паузы (куски для модели) → расшифровка (воркер «Монтажа», GPU-замок) → файлы

Результат в каталоге задачи: words.json (слова со временем), text.txt, text.md, subtitles.srt, subtitles.vtt.
Модели, их установка и воркер — те же, что у «Монтажа» (montage/asr.py).
"""

from __future__ import annotations

import copy
import json
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

from aiplatform.core import log, media, runner, schema
from aiplatform.core import uploads as uploads_module
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.gate import Gate
from aiplatform.montage import asr, stages, transcript
from aiplatform.montage.config import MontageConfig
from aiplatform.transcribe import formats

SCHEMA = 1
FIELDS = schema.require("id", "status", "created_at", "progress", types={"id": str, "status": str})
ACTIVE = ("queued", "running")
LOG_TAIL = 100
GPU_OWNER = "Расшифровка"
MAX_TITLE = 120
MAX_TERMS = 2000
PREVIEW_CHARS = 160
STAGES = {
    "audio": ("Звук", 3),
    "silences": ("Паузы", 2),
    "transcribe": ("Расшифровка", 90),
    "files": ("Текст и субтитры", 1),
}
# Файлы результата: имя на диске → (тип, расширение в имени скачиваемого файла).
FILES = {
    "text.txt": ("text/plain; charset=utf-8", "txt"),
    "text.md": ("text/markdown; charset=utf-8", "md"),
    "subtitles.srt": ("application/x-subrip", "srt"),
    "subtitles.vtt": ("text/vtt", "vtt"),
    "words.json": ("application/json", "json"),
}
VIDEO_EXT = uploads_module.VIDEO_EXT
AUDIO_EXT = ("mp3", "wav", "m4a", "ogg", "flac")
ACCEPTED = "Поддерживаются видео MP4, MOV, WebM и аудио MP3, WAV, M4A, OGG, FLAC."
DEFAULT_OPTIONS = {"model": asr.DEFAULT_ID, "language": "ru", "terms": ""}
_ID = re.compile(r"^[0-9a-f]{12}$")


class ValidationError(ValueError):
    pass


class Conflict(ValueError):
    """Действие не к месту: задача занята или в ней нет файла."""


def sniff(data: bytes) -> str | None:
    """Расширение по сигнатуре: видео (MP4/MOV/WebM) или аудио (MP3/WAV/M4A/OGG/FLAC)."""
    if ext := uploads_module.sniff_video(data):
        return ext
    if data.startswith(b"OggS"):
        return "ogg"
    if data.startswith(b"fLaC"):
        return "flac"
    ext = uploads_module.sniff(data)
    return ext if ext in AUDIO_EXT else None


def clean_options(payload: object, base: dict | None = None) -> dict:
    data = payload if isinstance(payload, dict) else {}
    out = dict(base or DEFAULT_OPTIONS)
    if "model" in data:
        if asr.get(data["model"]) is None:
            raise ValidationError("Неизвестная модель расшифровки.")
        out["model"] = data["model"]
    if "language" in data:
        if data["language"] not in asr.LANGUAGES:
            raise ValidationError(f"Язык — один из: {', '.join(asr.LANGUAGES)}.")
        out["language"] = data["language"]
    if "terms" in data:
        if not isinstance(data["terms"], str) or len(data["terms"]) > MAX_TERMS:
            raise ValidationError(f"Термины — строка до {MAX_TERMS} знаков.")
        out["terms"] = data["terms"].strip()
    return out


def new_stages(ids: list[str]) -> list[dict]:
    return [
        {"id": i, "label": STAGES[i][0], "weight": STAGES[i][1], "status": "pending", "percent": 0.0, "detail": "",
         "started_at": None, "finished_at": None}
        for i in ids
    ]  # fmt: skip


def asr_hint() -> str:
    return "Окружение расшифровки не найдено: make models M=whisper-large-v3-turbo в app/platform."


class Transcripts:
    def __init__(self, config: MontageConfig, data_dir: Path, gate: Gate, url_base: str = "/api/transcribe") -> None:
        self.config = config
        self.jobs_dir = data_dir / "jobs"
        self._gate = gate
        self._url = url_base
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        # Элемент очереди несёт своё событие отмены: отменённая и запущенная заново задача не запустит старое.
        self._queue: queue.Queue[tuple[str, threading.Event] | None] = queue.Queue()
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.load_problems: list[str] = []
        self._load()
        self._worker = threading.Thread(target=self._work, name="transcribe-worker", daemon=True)
        self._worker.start()

    # ---------- чтение ----------

    def dir(self, jid: str) -> Path:
        return self.jobs_dir / jid

    def get(self, jid: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(jid)
            return self._view(job, full=True) if job else None

    def list(self) -> list[dict]:
        with self._lock:
            items = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            return [self._view(j) for j in items]

    def _view(self, job: dict, *, full: bool = False) -> dict:
        view = copy.deepcopy(job)
        jid = job["id"]
        folder = self.dir(jid)
        base = f"{self._url}/jobs/{jid}"
        view["source_url"] = f"{base}/source" if job.get("source") else None
        view["files"] = {
            FILES[name][1]: f"{base}/files/{name}" for name in FILES if (folder / name).is_file()
        }  # fmt: skip
        if full:
            text = folder / "text.txt"
            view["text"] = text.read_text(encoding="utf-8") if text.is_file() else ""
            view["paragraphs"] = formats.paragraphs(self.words(jid)) if view["text"] else []
        else:
            view.pop("log", None)
        now = time.time()
        view["eta_s"] = stages.remaining(view["stages"], now) if job["status"] == "running" else None
        for stage in view["stages"]:
            stage["eta_s"] = stages.stage_remaining(stage, now)
        if job["status"] == "queued":
            mine = job.get("queued_at", 0)
            view["queue_position"] = sum(
                1 for j in self._jobs.values() if j["status"] in ACTIVE and j.get("queued_at", 0) < mine
            )
        return view

    def words(self, jid: str) -> list[dict]:
        try:
            data = json.loads((self.dir(jid) / "words.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data.get("words") or []

    def file(self, jid: str, name: str) -> Path | None:
        if jid not in self._jobs or name not in FILES:
            return None
        path = self.dir(jid) / name
        return path if path.is_file() else None

    def source_file(self, jid: str) -> tuple[Path, str] | None:
        with self._lock:
            job = self._jobs.get(jid)
            meta = job.get("source") if job else None
        if not meta:
            return None
        path = self.dir(jid) / f"source.{meta['ext']}"
        return (path, meta["ext"]) if path.is_file() else None

    # ---------- запись ----------

    def create(self, payload: object) -> dict:
        data = payload if isinstance(payload, dict) else {}
        title = data.get("title", "")
        if not isinstance(title, str) or len(title.strip()) > MAX_TITLE:
            raise ValidationError(f"Название — строка до {MAX_TITLE} знаков.")
        options = clean_options(data.get("options"), DEFAULT_OPTIONS | {"model": asr.default_id(self.config)})
        jid = uuid.uuid4().hex[:12]
        now = time.time()
        job = {
            "id": jid,
            "title": " ".join(title.split()) or time.strftime("Запись %d.%m %H:%M"),
            "status": "draft",
            "created_at": now,
            "updated_at": now,
            "backend": self.config.backend,
            "options": options,
            "source": None,
            "result": None,
            "progress": {"stage": "Ждёт файл", "percent": 0.0},
            "stages": [],
            "error": None,
            "log": [],
        }
        with self._lock:
            self._jobs[jid] = job
            self._cancels[jid] = threading.Event()
            self._save(job)
            return self._view(job)

    def update(self, jid: str, payload: object) -> dict | None:
        data = payload if isinstance(payload, dict) else {}
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return None
            if "title" in data:
                title = data["title"]
                if not isinstance(title, str) or not title.strip() or len(title) > MAX_TITLE:
                    raise ValidationError(f"Название — непустая строка до {MAX_TITLE} знаков.")
                job["title"] = " ".join(title.split())
            if "options" in data:
                self._idle(job)
                job["options"] = clean_options(data["options"], job["options"])
            self._touch(job)
            return self._view(job)

    def save_source(self, jid: str, tmp: Path, name: str = "") -> dict | None:
        """Файл (уже на диске, body_to_file) → source.<ext>; прошлый результат стирается."""
        try:
            with self._lock:
                job = self._jobs.get(jid)
                if job is None:
                    return None
                self._idle(job)
            ext = sniff(uploads_module.head(tmp)) if tmp.stat().st_size else None
            if ext is None:
                raise ValidationError(ACCEPTED)
            meta = self._probe(tmp)
            folder = self.dir(jid)
            for old in folder.glob("source.*"):
                old.unlink()
            for result in FILES:
                (folder / result).unlink(missing_ok=True)
            tmp.replace(folder / f"source.{ext}")
        finally:
            tmp.unlink(missing_ok=True)
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return None
            job["source"] = {**meta, "ext": ext, "kind": "video" if ext in VIDEO_EXT else "audio", "name": name[:200]}
            job["result"] = None
            job["progress"] = {"stage": "Файл загружен", "percent": 0.0}
            self._touch(job)
            return self._view(job)

    def start(self, jid: str, payload: object = None) -> dict | None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return None
            options = clean_options(payload, job["options"])
            self._idle(job)
            if not job.get("source"):
                raise Conflict("Сначала загрузи видео или аудио.")
            model = asr.get(options["model"]) or asr.CATALOG[0]
            if model.id in self.config.disabled_models():
                raise Conflict(f"Модель {model.label} выключена во вкладке «Настройки» — выбери другую.")
            if not asr.installed(model, self.config):
                raise Conflict(f"Модель {model.label} не установлена: make models M={model.id}.")
            event = threading.Event()
            self._cancels[jid] = event
            job.update(options=options, status="queued", error=None, log=[])
            job["progress"] = {"stage": "В очереди", "percent": 0.0}
            job["stages"] = new_stages(list(STAGES))
            job["queued_at"] = time.time()
            self._touch(job)
            self._queue.put((jid, event))
            return self._view(job)

    def cancel(self, jid: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return None
            if job["status"] == "queued":
                self._finish(job, "canceled")
            elif job["status"] == "running":
                job["progress"]["stage"] = "Отмена…"
            event = self._cancels.get(jid)
            if event:
                event.set()
            return self._view(job)

    def delete(self, jid: str) -> bool:
        with self._lock:
            job = self._jobs.get(jid)
            if job is None:
                return False
            if job["status"] in ACTIVE:
                raise Conflict("Задача в работе — сначала отмени.")
            del self._jobs[jid]
            self._cancels.pop(jid, None)
        shutil.rmtree(self.dir(jid), ignore_errors=True)
        return True

    def close(self) -> None:
        with self._lock:
            for event in self._cancels.values():
                event.set()
        self._queue.put(None)
        self._worker.join(timeout=15)

    # ---------- внутреннее: состояние ----------

    def _idle(self, job: dict) -> None:
        if job["status"] in ACTIVE:
            raise Conflict("Задача в работе — дождись или отмени.")

    def _touch(self, job: dict) -> None:
        job["updated_at"] = time.time()
        self._save(job)

    def _save(self, job: dict) -> None:
        folder = self.dir(job["id"])
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / "job.json", json.dumps(schema.stamp(job, SCHEMA), ensure_ascii=False, indent=2))

    def _load(self) -> None:
        for path in self.jobs_dir.glob("*/job.json"):
            if not _ID.match(path.parent.name):
                continue
            label = f"Расшифровка {path.parent.name}"
            job, _ = schema.load_record(path, SCHEMA, check=FIELDS, problems=self.load_problems, label=label)
            if job is None:
                continue
            job.setdefault("stages", [])
            job.setdefault("log", [])
            if job["status"] in ACTIVE:
                job["status"] = "failed"
                job["error"] = "Сервис был перезапущен во время работы — запусти ещё раз."
                job["progress"]["stage"] = "Ошибка"
                stages.finish(job["stages"], "failed")
                self._save(job)
            self._jobs[job["id"]] = job
            self._cancels[job["id"]] = threading.Event()

    def _finish(self, job: dict, status: str, *, error: str | None = None) -> None:
        job["status"] = status
        job["error"] = error
        stages.finish(job.setdefault("stages", []), status)
        if status == "done":
            job["progress"] = {"stage": "Готово", "percent": 100.0}
        else:
            job["progress"]["stage"] = "Ошибка" if status == "failed" else "Отменено"
        self._touch(job)

    def _stage(self, job: dict, cancel: threading.Event, stage_id: str, percent: float, detail: str = "") -> None:
        with self._lock:
            if cancel.is_set():
                return
            stages.advance(job["stages"], stage_id, percent, detail)
            label = STAGES[stage_id][0]
            text = f"{label}: {detail}" if detail else label
            job["progress"] = {"stage": text, "percent": stages.overall(job["stages"])}

    def _note(self, job: dict, line: str) -> None:
        with self._lock:
            job["log"].append(line.rstrip()[:500])
            del job["log"][:-LOG_TAIL]

    # ---------- внутреннее: воркер ----------

    def _work(self) -> None:
        while (item := self._queue.get()) is not None:
            jid, event = item
            with self._lock:
                job = self._jobs.get(jid)
                cancel = self._cancels.get(jid)
                if job is None or cancel is not event or job["status"] != "queued":
                    continue
                job["status"] = "running"
                job["progress"] = {"stage": "Запуск", "percent": 0.0}
                self._touch(job)
            self._run(job, cancel)

    def _run(self, job: dict, cancel: threading.Event) -> None:
        try:
            self._transcribe(job, cancel)
            if cancel.is_set():
                raise runner.Canceled
        except runner.Canceled:
            with self._lock:
                self._finish(job, "canceled")
        except (runner.RunFailed, ValueError) as exc:
            with self._lock:
                self._finish(job, "failed", error=str(exc))
        except Exception as exc:  # noqa: BLE001 — воркер не должен умереть из-за одной задачи
            with self._lock:
                log.internal_error(exc, task=job["id"])
                job["log"].append(f"✗ {type(exc).__name__}: {exc}")
                self._finish(job, "failed", error=log.TASK_INTERNAL)
        else:
            with self._lock:
                self._finish(job, "done")
        finally:
            (self.dir(job["id"]) / "audio.wav").unlink(missing_ok=True)

    def _probe(self, path: Path) -> dict:
        argv = [media.ffprobe_for(self.config.ffmpeg), "-v", "error", "-show_entries",
                "format=duration:stream=codec_type", "-of", "json", str(path)]  # fmt: skip
        try:
            info = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout)
        except (subprocess.SubprocessError, ValueError, OSError) as exc:
            raise ValidationError("Файл не читается как видео или аудио.") from exc
        if not any(s.get("codec_type") == "audio" for s in info.get("streams") or []):
            raise ValidationError("В файле нет звука — расшифровывать нечего.")
        duration = float((info.get("format") or {}).get("duration") or 0)
        if duration <= 0:
            raise ValidationError("Не удалось узнать длительность записи.")
        return {"duration": round(duration, 3), "size": path.stat().st_size}

    def _transcribe(self, job: dict, cancel: threading.Event) -> None:
        jid = job["id"]
        folder = self.dir(jid)
        found = self.source_file(jid)
        if found is None:
            raise runner.RunFailed("Файл пропал из каталога задачи — загрузи его заново.")
        self._stage(job, cancel, "audio", 0)
        wav = folder / "audio.wav"
        lines: list[str] = []
        argv = [self.config.ffmpeg, "-y", "-loglevel", "error", "-i", str(found[0]), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(wav)]  # fmt: skip
        try:
            runner.run(argv, lines.append, cancel, kind="render")
        except runner.RunFailed as exc:
            raise runner.RunFailed(f"ffmpeg (звук): {(lines or [str(exc)])[-1].strip()}") from exc
        self._stage(job, cancel, "silences", 0)
        duration = media.duration(wav, self.config.ffmpeg)
        lines = []
        runner.run(transcript.silence_argv(self.config.ffmpeg, wav), lines.append, cancel, kind="render")
        silences = transcript.parse_silences(lines, duration)
        options = job["options"]
        model = asr.get(options["model"]) or asr.CATALOG[0]
        hotwords = [t.strip() for t in re.split(r"[,\n;]", options["terms"]) if t.strip()]
        out = folder / "words.json"
        argv = asr.build_argv(self.config, model, wav, out, splits=transcript.split_points(duration, silences),
                              hotwords=hotwords, language=options["language"])  # fmt: skip

        def on_line(line: str) -> None:
            step = asr.parse_progress(line)
            if step:
                self._stage(job, cancel, "transcribe", step[0], step[1] or model.label)
            elif line.strip():
                self._note(job, line)

        def on_wait(holder: str) -> None:
            self._stage(job, cancel, "transcribe", 0, f"ждёт GPU (занят: {holder})")

        with self._gate.hold(GPU_OWNER, on_wait, cancel):
            self._stage(job, cancel, "transcribe", 0, model.label)
            runner.run(argv, on_line, cancel, missing_hint=asr_hint(), kind="asr")
        try:
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise runner.RunFailed("Расшифровка завершилась, но слов нет: файл результата не прочитался.") from exc
        words = [w for w in data.get("words") or [] if isinstance(w, dict) and str(w.get("w", "")).strip()]
        if not words:
            out.unlink(missing_ok=True)
            raise runner.RunFailed("В записи не нашлось речи.")
        self._stage(job, cancel, "files", 0)
        payload = {"model": model.id, "language": options["language"], "duration": duration, "words": words}
        atomic_write(out, json.dumps(payload, ensure_ascii=False))
        atomic_write(folder / "text.txt", formats.txt(words))
        atomic_write(folder / "text.md", formats.md(words, job["title"]))
        atomic_write(folder / "subtitles.srt", formats.srt(words))
        atomic_write(folder / "subtitles.vtt", formats.vtt(words))
        text = " ".join(w["w"] for w in words)
        with self._lock:
            job["result"] = {
                "model": model.id,
                "words": len(words),
                "chars": len(text),
                "elapsed_s": data.get("elapsed_s"),
                "preview": text[:PREVIEW_CHARS],
            }
            self._touch(job)
