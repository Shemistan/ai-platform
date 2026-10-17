"""Задачи озвучки: текст → mp3 через `narrate say`, очередь с одним воркером, GPU-замок."""

from __future__ import annotations

import copy
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiplatform.audio import voices
from aiplatform.audio.config import AudioConfig
from aiplatform.core import cleanup, media, runner
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.gate import Gate
from aiplatform.core.narrator import INSTALL_HINT

ACTIVE = ("queued", "running")
LOG_TAIL = 200
MAX_TEXT = 100_000
PREVIEW = 160
SOURCE = "source.txt"
OUTPUT = "output.mp3"
CHECK = "check.json"  # отчёт проверки распознаванием от `say --check-json`
STEPS_RANGE = (1, 128)
TITLE_CHARS = 90
GPU_OWNER = "Аудио"
WAIT_POLL_S = 0.2
# Темп скачиваемого mp3: atempo берёт 0,5–2 одним фильтром.
TEMPO_RANGE = (0.5, 2.0)
RETIME_TIMEOUT_S = 600
_ID = re.compile(r"^[0-9a-f]{12}$")
_STAGE = re.compile(r"абзац (\d+)/(\d+)")
_DONE = re.compile(r"^\[(\d+)/(\d+)\]")


class ValidationError(ValueError):
    """Запрос не годится; текст показывается пользователю."""


class RetimeFailed(RuntimeError):
    """ffmpeg не собрал mp3 в другом темпе."""


def parse_tempo(raw: str | None) -> float:
    """Темп скачиваемого mp3 из ?speed=: пусто — 1, иначе число в TEMPO_RANGE."""
    if raw in (None, ""):
        return 1.0
    try:
        tempo = round(float(raw.replace(",", ".")), 2)
    except ValueError:
        raise ValidationError("Скорость — число.") from None
    low, high = TEMPO_RANGE
    if not low <= tempo <= high:
        raise ValidationError(f"Скорость скачивания — от {low:g} до {high:g}.")
    return tempo


@dataclass(frozen=True)
class AudioSpec:
    text: str
    engine: str
    voice: str
    speed: float | None
    steps: int | None = None
    verify: bool = False  # проверить распознаванием после озвучки
    title: str = ""  # название в истории; пусто — начало текста


def build_spec(
    payload: object, default_engine: str = voices.DEFAULT_ENGINE, engines: dict[str, voices.Engine] | None = None
) -> AudioSpec:
    """engines — включённые движки и голоса; по умолчанию весь список voices.ENGINES."""
    engines = voices.ENGINES if engines is None else engines
    if not isinstance(payload, dict):
        raise ValidationError("Тело запроса — не объект JSON.")
    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValidationError("Нужен текст для озвучки.")
    if len(text) > MAX_TEXT:
        raise ValidationError(f"Текст длиннее {MAX_TEXT:,} знаков — раздели его.".replace(",", " "))
    engine_id = payload.get("engine") or default_engine
    engine = engines.get(engine_id) if isinstance(engine_id, str) else None
    if engine is None:
        raise ValidationError(voices.unknown(engine_id))
    voice = payload.get("voice") or engine.default_voice
    if voice not in engine.voices:
        raise ValidationError(voices.unknown(engine.id, voice))
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
    steps = payload.get("steps")
    if steps in (None, ""):
        steps = None
    else:
        if isinstance(steps, bool) or not isinstance(steps, int):
            raise ValidationError("Шаги — целое число.")
        low, high = STEPS_RANGE
        if not low <= steps <= high:
            raise ValidationError(f"Шаги — от {low} до {high}.")
        steps = steps if engine.tunable else None
    title = payload.get("title")
    title = " ".join(title.split())[:TITLE_CHARS] if isinstance(title, str) else ""
    return AudioSpec(
        text=text.strip() + "\n",
        engine=engine.id,
        voice=voice,
        speed=speed,
        steps=steps,
        verify=bool(payload.get("verify")),
        title=title,
    )


class Progress:
    """Строки `narrate say` вне терминала: «<title> абзац n/N» и «[i/N] …» по готовым абзацам."""

    def __init__(self, verify: bool = False) -> None:
        self.done = 0
        self.total = 0
        self.stage = "Загрузка модели"
        self.verify = verify
        self.checking = False

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
        elif self.verify and ".mp3  " in line:  # mp3 готов, дальше — распознавание
            self.checking = True
            self.stage = "Проверка распознаванием"

    def snapshot(self) -> dict:
        share = 80.0 if self.verify else 95.0
        percent = round(share * self.done / self.total, 1) if self.total else 0.0
        if self.checking:
            percent = share
        return {"stage": self.stage, "step": self.done or None, "steps": self.total or None, "percent": percent}


class AudioJobs:
    def __init__(self, config: AudioConfig, gate: Gate, url_base: str = "/api/audio") -> None:
        self._config = config
        self._gate = gate
        self._url = url_base
        self._lock = threading.Lock()
        self._retime_lock = threading.Lock()
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

    def forget(self, job_id: str) -> int:
        """Уборка: удалить завершённую задачу из истории и с диска; идущую — нельзя. Возвращает байты."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] in ACTIVE:
                return 0
            del self._jobs[job_id]
        return cleanup.remove_tree(self._dir(job_id))

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            return [{k: v for k, v in self._view(j).items() if k != "log"} for j in jobs]

    def audio_path(self, job_id: str, tempo: float = 1.0) -> Path | None:
        """Готовый mp3; tempo ≠ 1 — его копия в другом темпе, собирается при первом запросе."""
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "done":
                return None
        path = self._dir(job_id) / OUTPUT
        if not path.is_file():
            return None
        if tempo == 1.0:
            return path
        out = path.with_name(f"{path.stem}@{tempo:g}{path.suffix}")
        with self._retime_lock:
            if not out.is_file():
                self._retime(path, out, tempo)
        return out

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
        return self.submit(build_spec(payload, self._config.default_engine, self._config.engines()))

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
            "steps": spec.steps,
            "verify": spec.verify,
            "title": spec.title,
            "chars": len(spec.text.strip()),
            "preview": " ".join(spec.text.split())[:PREVIEW],
            "duration_s": None,
            "check": None,
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

    def _retime(self, audio: Path, out: Path, tempo: float) -> None:
        """Как retime у студии: длина делится на tempo, высота голоса та же."""
        tmp = out.with_name(out.stem + ".part.mp3")
        argv = [self._config.ffmpeg, "-y", "-loglevel", "error", "-i", str(audio), "-filter:a", f"atempo={tempo:g}"]
        argv += ["-codec:a", "libmp3lame", "-b:a", "128k", str(tmp)]
        try:
            subprocess.run(argv, capture_output=True, text=True, check=True, timeout=RETIME_TIMEOUT_S)
        except (OSError, subprocess.SubprocessError) as exc:
            tmp.unlink(missing_ok=True)
            detail = getattr(exc, "stderr", None) or str(exc)
            raise RetimeFailed(f"Не удалось ускорить mp3: {detail.strip()[-300:]}") from exc
        tmp.replace(out)

    def _save(self, job: dict) -> None:
        folder = self._dir(job["id"])
        folder.mkdir(parents=True, exist_ok=True)
        atomic_write(folder / "job.json", json.dumps(job, ensure_ascii=False, indent=2))

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
        if spec.steps is not None:
            argv += ["--steps", str(spec.steps)]
        if spec.verify:
            return [*argv, "--out", str(output), "--check-json", str(output.with_name(CHECK))]
        return [*argv, "--out", str(output), "--no-verify"]

    def _run(self, job: dict, spec: AudioSpec, cancel: threading.Event) -> None:
        folder = self._dir(job["id"])
        output = folder / OUTPUT
        parser = Progress(spec.verify)

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
                narrator = self._config.narrator
                argv = self.argv(spec, folder / SOURCE, output)
                runner.run(argv, on_line, cancel, cwd=narrator.cwd, env=narrator.env, missing_hint=INSTALL_HINT)
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
            check = _read_check(folder / CHECK) if spec.verify else None
            with self._lock:
                job["duration_s"] = round(duration, 2) if duration else None
                job["check"] = check
                self._finish(job, "done")


def _read_check(path: Path) -> dict | None:
    """Отчёт проверки; нет файла — проверку пропустили (нет модели распознавания)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"skipped": "отчёта проверки нет — модель распознавания не найдена?"}
    return data if isinstance(data, dict) else None


def _error_text(reason: str, log: list[str]) -> str:
    for line in reversed(log):
        text = line.strip()
        if text and not text.startswith("["):
            return f"{reason} Последнее сообщение: {text}"
    return reason
