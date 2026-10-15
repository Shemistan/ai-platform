"""Задачи озвучки: текст → mp3 через `narrate say`, очередь с одним воркером, GPU-замок."""

from __future__ import annotations

import copy
import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiplatform.audio import voices
from aiplatform.audio.config import AudioConfig
from aiplatform.core import media, runner
from aiplatform.core.gate import Gate

ACTIVE = ("queued", "running")
LOG_TAIL = 200
MAX_TEXT = 100_000
PREVIEW = 160
SOURCE = "source.txt"
OUTPUT = "output.mp3"
GPU_OWNER = "Аудио"
WAIT_POLL_S = 0.2
INSTALL_HINT = "Нужен narrator: 07_offline_text-to-word, `make models` (или задай AIP_NARRATE_CMD)."
_ID = re.compile(r"^[0-9a-f]{12}$")
_STAGE = re.compile(r"абзац (\d+)/(\d+)")
_DONE = re.compile(r"^\[(\d+)/(\d+)\]")


class ValidationError(ValueError):
    """Запрос не годится; текст показывается пользователю."""


@dataclass(frozen=True)
class AudioSpec:
    text: str
    engine: str
    voice: str
    speed: float | None


def build_spec(payload: object, default_engine: str = voices.DEFAULT_ENGINE) -> AudioSpec:
    if not isinstance(payload, dict):
        raise ValidationError("Тело запроса — не объект JSON.")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("Нужен текст для озвучки.")
    if len(text) > MAX_TEXT:
        raise ValidationError(f"Текст длиннее {MAX_TEXT:,} знаков — раздели его.".replace(",", " "))
    engine_id = payload.get("engine") or default_engine
    engine = voices.ENGINES.get(engine_id) if isinstance(engine_id, str) else None
    if engine is None:
        raise ValidationError("Неизвестный движок озвучки.")
    voice = payload.get("voice") or engine.default_voice
    if voice not in engine.voices:
        raise ValidationError(f"У движка {engine.label} нет голоса {voice!r}.")
    speed = payload.get("speed")
    if speed in (None, ""):
        speed = None
    else:
        if isinstance(speed, bool) or not isinstance(speed, int | float):
            raise ValidationError("Скорость — число.")
        low, high = voices.SPEED_RANGE
        if not low <= speed <= high:
            raise ValidationError(f"Скорость — от {low:g} до {high:g}.")
        speed = float(speed) if engine.tunable else None
    return AudioSpec(text=text.strip() + "\n", engine=engine.id, voice=voice, speed=speed)


class Progress:
    """Строки `narrate say` вне терминала: «<title> абзац n/N» и «[i/N] …» по готовым абзацам."""

    def __init__(self) -> None:
        self.done = 0
        self.total = 0
        self.stage = "Загрузка модели"

    def feed(self, line: str) -> None:
        if m := _STAGE.search(line):
            n, total = int(m[1]), int(m[2])
            self.total = total
            self.stage = f"Абзац {n} из {total}"
        elif m := _DONE.match(line.strip()):
            self.done = max(self.done, int(m[1]))
            self.total = max(self.total, int(m[2]))
        elif "склеиваю" in line:
            self.stage = "Склейка и mp3"

    def snapshot(self) -> dict:
        percent = round(95.0 * self.done / self.total, 1) if self.total else 0.0
        return {"stage": self.stage, "step": self.done or None, "steps": self.total or None, "percent": percent}


class AudioJobs:
    def __init__(self, config: AudioConfig, gate: Gate, url_base: str = "/api/audio") -> None:
        self._config = config
        self._gate = gate
        self._url = url_base
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._specs: dict[str, AudioSpec] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        config.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load()
        self._worker = threading.Thread(target=self._work, name="audio-worker", daemon=True)
        self._worker.start()

    # --- чтение ---

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._view(job) if job else None

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            return [{k: v for k, v in self._view(j).items() if k != "log"} for j in jobs]

    def audio_path(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "done":
                return None
        path = self._dir(job_id) / OUTPUT
        return path if path.is_file() else None

    def text(self, job_id: str) -> str | None:
        if not _ID.match(job_id or ""):
            return None
        try:
            return (self._dir(job_id) / SOURCE).read_text(encoding="utf-8")
        except OSError:
            return None

    def _view(self, job: dict) -> dict:
        view = copy.deepcopy(job)
        if job["status"] == "queued":
            ahead = [j for j in self._jobs.values() if j["status"] in ACTIVE and j["created_at"] < job["created_at"]]
            view["queue_position"] = len(ahead)
        end = job["finished_at"] or time.time()
        view["elapsed_s"] = round(end - job["started_at"], 1) if job["started_at"] else 0.0
        view["audio_url"] = f"{self._url}/jobs/{job['id']}/audio" if job["status"] == "done" else None
        return view

    # --- запись ---

    def create(self, payload: object) -> dict:
        return self.submit(build_spec(payload, self._config.default_engine))

    def submit(self, spec: AudioSpec) -> dict:
        engine = voices.ENGINES[spec.engine]
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "engine": engine.id,
            "engine_label": engine.label,
            "voice": spec.voice,
            "speed": spec.speed,
            "chars": len(spec.text.strip()),
            "preview": " ".join(spec.text.split())[:PREVIEW],
            "duration_s": None,
            "progress": {"stage": "В очереди", "step": None, "steps": None, "percent": 0.0},
            "error": None,
            "log": [],
        }
        folder = self._dir(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / SOURCE).write_text(spec.text, encoding="utf-8")
        with self._lock:
            self._jobs[job_id] = job
            self._cancels[job_id] = threading.Event()
            self._specs[job_id] = spec
            self._save(job)
            view = self._view(job)
        self._queue.put(job_id)
        return view

    def wait(self, job_id: str, cancel: threading.Event | None = None) -> dict:
        """Ждать конца задачи; взведённый cancel отменяет и её."""
        while True:
            job = self.get(job_id)
            if job is None:
                raise ValidationError("Задача пропала.")
            if job["status"] not in ACTIVE:
                return job
            if cancel is not None and cancel.wait(WAIT_POLL_S):
                return self.cancel(job_id) or job
            if cancel is None:
                time.sleep(WAIT_POLL_S)

    def cancel(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if job["status"] == "queued":
                self._finish(job, "canceled", stage="Отменено")
            elif job["status"] == "running":
                job["progress"]["stage"] = "Отмена…"
            event = self._cancels.get(job_id)
            if event:
                event.set()
            return self._view(job)

    def close(self) -> None:
        with self._lock:
            for event in self._cancels.values():
                event.set()
        self._queue.put(None)
        self._worker.join(timeout=15)

    # --- внутреннее ---

    def _dir(self, job_id: str) -> Path:
        return self._config.jobs_dir / job_id

    def _save(self, job: dict) -> None:
        folder = self._dir(job["id"])
        folder.mkdir(parents=True, exist_ok=True)
        tmp = folder / "job.json.tmp"
        tmp.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(folder / "job.json")

    def _load(self) -> None:
        for path in self._config.jobs_dir.glob("*/job.json"):
            if not _ID.match(path.parent.name):
                continue
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if job.get("status") in ACTIVE:
                self._finish(job, "failed", error="Сервис был перезапущен во время озвучки.")
            self._jobs[job["id"]] = job

    def _finish(self, job: dict, status: str, *, error: str | None = None, stage: str | None = None) -> None:
        job["status"] = status
        job["finished_at"] = time.time()
        job["error"] = error
        if status == "done":
            job["progress"] = {"stage": "Готово", "step": None, "steps": None, "percent": 100.0}
        else:
            job["progress"]["stage"] = stage or ("Ошибка" if status == "failed" else "Отменено")
        self._save(job)

    def _work(self) -> None:
        while (job_id := self._queue.get()) is not None:
            with self._lock:
                job = self._jobs[job_id]
                spec = self._specs.pop(job_id)
                cancel = self._cancels[job_id]
                if job["status"] != "queued":
                    self._cancels.pop(job_id, None)
                    continue
                job["status"] = "running"
                job["started_at"] = time.time()
                job["progress"]["stage"] = "Запуск"
                self._save(job)
            self._run(job, spec, cancel)
            with self._lock:
                self._cancels.pop(job_id, None)

    def argv(self, spec: AudioSpec, source: Path, output: Path) -> list[str]:
        argv = [*self._config.narrate_cmd, "say", str(source), "--engine", spec.engine, "--voice", spec.voice]
        if spec.speed is not None:
            argv += ["--speed", f"{spec.speed:g}"]
        return [*argv, "--out", str(output), "--no-verify"]

    def _run(self, job: dict, spec: AudioSpec, cancel: threading.Event) -> None:
        folder = self._dir(job["id"])
        output = folder / OUTPUT
        parser = Progress()

        def on_line(line: str) -> None:
            parser.feed(line)
            with self._lock:
                if not cancel.is_set():
                    job["progress"] = parser.snapshot()
                if line.strip():
                    job["log"].append(line.rstrip())
                    del job["log"][:-LOG_TAIL]

        def on_wait(holder: str) -> None:
            with self._lock:
                job["progress"]["stage"] = f"Ждёт GPU (занят: {holder})"
                self._save(job)

        try:
            with self._gate.hold(GPU_OWNER, on_wait, cancel):
                with self._lock:
                    job["progress"]["stage"] = "Загрузка модели"
                    job["started_at"] = time.time()
                cwd = self._config.narrator_dir if self._config.narrator_dir.is_dir() else None
                argv = self.argv(spec, folder / SOURCE, output)
                runner.run(argv, on_line, cancel, cwd=cwd, missing_hint=INSTALL_HINT)
            if not output.is_file() or output.stat().st_size == 0:
                raise runner.RunFailed("Озвучка завершилась без ошибки, но mp3 не появился.")
            duration = media.duration(output, self._config.ffmpeg)
        except runner.Canceled:
            output.unlink(missing_ok=True)
            with self._lock:
                self._finish(job, "canceled")
        except runner.RunFailed as exc:
            with self._lock:
                self._finish(job, "failed", error=_error_text(str(exc), job["log"]))
        except Exception as exc:  # noqa: BLE001 — воркер не должен умереть из-за одной задачи
            with self._lock:
                self._finish(job, "failed", error=f"Внутренняя ошибка сервиса: {exc!r}")
        else:
            with self._lock:
                job["duration_s"] = round(duration, 2) if duration else None
                self._finish(job, "done")


def _error_text(reason: str, log: list[str]) -> str:
    for line in reversed(log):
        text = line.strip()
        if text and not text.startswith("["):
            return f"{reason} Последнее сообщение: {text}"
    return reason
