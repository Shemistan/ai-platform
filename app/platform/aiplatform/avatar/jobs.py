"""Задачи диктора: озвучка → анимация персонажа → mp4. Хранение на диске, очередь, один воркер.

Озвучку делает очередь «Аудио» (она сама берёт GPU-замок); SadTalker идёт
под замком здесь. Картинка персонажа и озвучка копируются в каталог задачи
при создании: удалённый персонаж или убранная загрузка задачу не ломают.
"""

from __future__ import annotations

import copy
import json
import queue
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiplatform.audio.jobs import AudioJobs, AudioSpec
from aiplatform.audio.jobs import ValidationError as AudioValidationError
from aiplatform.audio.jobs import build_spec as audio_spec
from aiplatform.avatar import models, sadtalker
from aiplatform.avatar.characters import CharacterStore
from aiplatform.avatar.config import AvatarConfig
from aiplatform.core import cleanup, log, media, runner, schema
from aiplatform.core import jobs as core_jobs
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.gate import Gate
from aiplatform.core.schema import JOB_FIELDS, JOB_SCHEMA
from aiplatform.core.uploads import UploadStore
from aiplatform.studio.render import AI_METADATA

ACTIVE = core_jobs.ACTIVE
LOG_TAIL = 200
PREVIEW = 160
GPU_OWNER = "Диктор"
POLL_S = 0.3
INSTALL_HINT = "Поставь модель диктора: make models M=sadtalker в app/platform (README, «Установка моделей»)."
NO_FACE = (
    "На картинке персонажа не нашлось лица. Нужно лицо анфас, крупно и без перекрытий; "
    "рисованному персонажу — лицо в человеческих пропорциях."
)
SOURCES = ("text", "audio")
VOICE_SHARE = 30.0  # доля прогресса на озвучку, когда речь из текста
_ID = re.compile(r"^[0-9a-f]{12}$")


class ValidationError(ValueError):
    pass


@dataclass(frozen=True)
class AvatarSpec:
    voice: AudioSpec | None  # None — своя озвучка уже лежит в каталоге задачи
    size: int
    frame: str
    enhance: bool


class AvatarJobs:
    def __init__(
        self,
        config: AvatarConfig,
        uploads: UploadStore,
        characters: CharacterStore,
        gate: Gate,
        url_base: str = "/api/avatar",
    ) -> None:
        self._config = config
        self._uploads = uploads
        self._characters = characters
        self._gate = gate
        self._url = url_base
        # Очередь «Аудио» и её настройки (движки, голос по умолчанию) — их связывает AvatarModule.bind().
        self.audio: AudioJobs | None = None
        self.audio_config = None
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._specs: dict[str, AvatarSpec] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        config.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load()
        self._worker = threading.Thread(target=self._work, name="avatar-worker", daemon=True)
        self._worker.start()

    # --- чтение ---

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._view(job) if job else None

    def list(self, limit: int | None = None, since: float | None = None) -> list[dict]:
        with self._lock:
            jobs = core_jobs.select(self._jobs.values(), limit, since)
            return [{k: v for k, v in self._view(j).items() if k != "log"} for j in jobs]

    def forget(self, job_id: str) -> int:
        """Уборка: удалить завершённую задачу из истории и с диска; идущую — нельзя. Возвращает байты."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] in ACTIVE:
                return 0
            del self._jobs[job_id]
        return cleanup.remove_tree(self._dir(job_id))

    def video_path(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "done":
                return None
        path = self._dir(job_id) / "output.mp4"
        return path if path.is_file() else None

    def face_path(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
        if not job or not job.get("face"):
            return None
        path = self._dir(job_id) / job["face"]
        return path if path.is_file() else None

    def _view(self, job: dict) -> dict:
        view = copy.deepcopy(job)
        if job["status"] == "queued":
            view["queue_position"] = sum(
                1 for j in self._jobs.values() if j["status"] in ACTIVE and j["created_at"] < job["created_at"]
            )
        end = job["finished_at"] or time.time()
        view["elapsed_s"] = round(end - job["started_at"], 1) if job["started_at"] else 0.0
        view["video_url"] = f"{self._url}/jobs/{job['id']}/video" if job["status"] == "done" else None
        view["face_url"] = f"{self._url}/jobs/{job['id']}/face" if job.get("face") else None
        return view

    # --- запись ---

    def create(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise ValidationError("Тело запроса — не объект JSON.")
        character = self._characters.get(payload.get("character"))
        face = self._characters.image(payload.get("character")) if character else None
        if face is None:
            raise ValidationError("Выбери персонажа — или добавь его, если списка ещё нет.")
        model_id = payload.get("model") or models.DEFAULT_ID
        model = next((m for m in models.CATALOG if m.id == model_id), None)
        if model is None:
            raise ValidationError("Неизвестная модель диктора.")
        if not models.installed(model, self._config):
            raise ValidationError(f"Модель {model.label} не установлена: запусти make models M={model.id}.")
        size = payload.get("size", model.sizes[0])
        if size not in model.sizes:
            raise ValidationError(f"Качество — одно из: {', '.join(map(str, model.sizes))}.")
        frame = payload.get("frame", sadtalker.DEFAULT_FRAME)
        if frame not in sadtalker.FRAMES:
            raise ValidationError("Кадр — crop (голова крупно) или full (как на фото).")
        enhance = payload.get("enhance", False)
        if not isinstance(enhance, bool):
            raise ValidationError("enhance — true или false.")
        source = payload.get("source", "text")
        if source not in SOURCES:
            raise ValidationError("Речь — text (озвучить текст) или audio (своя озвучка).")

        voice: AudioSpec | None = None
        speech: Path | None = None
        if source == "text":
            if self.audio is None or self.audio_config is None:
                raise ValidationError("Вкладка «Аудио» не запущена — озвучить текст нечем.")
            try:
                voice = audio_spec(payload, self.audio_config.default_engine, self.audio_config.engines())
            except AudioValidationError as exc:
                raise ValidationError(str(exc)) from exc
        else:
            speech = self._uploads.path(payload.get("upload_id"))
            if speech is None or speech.suffix[1:] not in ("mp3", "wav", "m4a"):
                raise ValidationError("Загрузи озвучку — mp3, wav или m4a.")

        job_id = uuid.uuid4().hex[:12]
        folder = self._dir(job_id)
        folder.mkdir(parents=True, exist_ok=True)
        face_name = f"face{face.suffix}"
        shutil.copyfile(face, folder / face_name)
        if speech is not None:
            shutil.copyfile(speech, folder / f"voice{speech.suffix}")
        speaker = None
        if voice is not None:
            engine = self.audio_config.engines().get(voice.engine) if self.audio_config else None
            label = engine.label if engine else voice.engine
            speaker = {"engine": voice.engine, "engine_label": label, "voice": voice.voice}
        job = {
            "id": job_id,
            "kind": "avatar",
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "backend": self._config.backend,
            "character": {"id": character["id"], "name": character["name"], "kind": character["kind"]},
            "face": face_name,
            "model": model.id,
            "model_label": model.label,
            "size": size,
            "frame": frame,
            "enhance": enhance,
            "source": source,
            "voice": speaker,
            "preview": " ".join(voice.text.split())[:PREVIEW] if voice else "",
            "audio_job": None,
            "duration_s": None,
            "progress": {"stage": "В очереди", "step": None, "steps": None, "percent": 0.0},
            "error": None,
            "log": [],
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancels[job_id] = threading.Event()
            self._specs[job_id] = AvatarSpec(voice, size, frame, enhance)
            self._save(job)
            view = self._view(job)
        self._queue.put(job_id)
        return view

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
        atomic_write(folder / "job.json", json.dumps(schema.stamp(job, JOB_SCHEMA), ensure_ascii=False, indent=2))

    def _load(self) -> None:
        # Битая задача — в карантин (job.json.bad) и в problems вкладки; остальные грузятся (BE-008).
        self.load_problems: list[str] = []
        for path in self._config.jobs_dir.glob("*/job.json"):
            if not _ID.match(path.parent.name):
                continue
            label = f"Задача {path.parent.name}"
            job, _ = schema.load_record(path, JOB_SCHEMA, check=JOB_FIELDS, problems=self.load_problems, label=label)
            if job is None:
                continue
            if job.get("status") in ACTIVE:
                self._finish(job, "failed", error="Сервис был перезапущен во время подготовки видео.")
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

    def _progress(self, job: dict, cancel: threading.Event, stage: str, percent: float, **more) -> None:
        with self._lock:
            if not cancel.is_set():
                job["progress"] = {"stage": stage, "step": None, "steps": None, "percent": round(percent, 1)} | more

    def _note(self, job: dict, line: str) -> None:
        with self._lock:
            job["log"].append(line.rstrip())
            del job["log"][:-LOG_TAIL]

    def _work(self) -> None:
        while (job_id := self._queue.get()) is not None:
            with self._lock:
                job = self._jobs.get(job_id)
                spec = self._specs.pop(job_id, None)
                cancel = self._cancels.get(job_id)
                # Отменили, пока ждала в очереди, — или уже и забыли (уборка): пропустить, воркер жив (BE-007).
                if job is None or spec is None or cancel is None or job["status"] != "queued":
                    self._cancels.pop(job_id, None)
                    continue
                job["status"] = "running"
                job["started_at"] = time.time()
                job["progress"]["stage"] = "Запуск"
                self._save(job)
            self._run(job, spec, cancel)
            with self._lock:
                self._cancels.pop(job_id, None)

    def _run(self, job: dict, spec: AvatarSpec, cancel: threading.Event) -> None:
        folder = self._dir(job["id"])
        try:
            speech = self._speak(job, spec, cancel) if spec.voice else next(folder.glob("voice.*"), None)
            if speech is None:
                raise runner.RunFailed("Файл озвучки пропал из каталога задачи — загрузи его заново.")
            wav = folder / "speech.wav"
            self._ffmpeg(["-i", str(speech), "-ac", "1", "-ar", "16000", str(wav)], cancel, "речь в wav")
            seconds = round(media.duration(wav, self._config.ffmpeg), 1)
            with self._lock:
                job["duration_s"] = seconds
            result = self._animate(job, spec, wav, cancel)
            output = folder / "output.mp4"
            # Метка ИИ в контейнере, без перекодирования (CMP-011); faststart — чтобы плеер начинал сразу.
            argv = ["-i", str(result), "-map", "0", "-c", "copy", *AI_METADATA, "-movflags", "+faststart", str(output)]
            self._ffmpeg(argv, cancel, "метка ИИ в mp4")
            shutil.rmtree(folder / "sadtalker", ignore_errors=True)
            wav.unlink(missing_ok=True)
        except runner.Canceled:
            (folder / "output.mp4").unlink(missing_ok=True)
            if job.get("audio_job") and self.audio is not None:
                self.audio.cancel(job["audio_job"])
            with self._lock:
                self._finish(job, "canceled")
        except runner.RunFailed as exc:
            with self._lock:
                self._finish(job, "failed", error=_error_text(str(exc), job["log"]))
        except Exception as exc:  # noqa: BLE001 — воркер не должен умереть из-за одной задачи
            with self._lock:
                log.internal_error(exc, task=job["id"])
                job["log"].append(f"✗ {type(exc).__name__}: {exc}")
                self._finish(job, "failed", error=log.TASK_INTERNAL)
        else:
            with self._lock:
                self._finish(job, "done")

    def _speak(self, job: dict, spec: AvatarSpec, cancel: threading.Event) -> Path:
        """Озвучка через очередь «Аудио»: её прогресс — первые VOICE_SHARE процентов задачи."""
        assert self.audio is not None and spec.voice is not None
        audio = self.audio.submit(spec.voice)
        with self._lock:
            job["audio_job"] = audio["id"]
            self._save(job)
        while audio["status"] in ACTIVE:
            if cancel.wait(POLL_S):
                raise runner.Canceled
            audio = self.audio.get(audio["id"]) or {"status": "failed", "error": "Задача озвучки пропала."}
            inner = audio.get("progress") or {}
            stage = inner.get("stage") or "в очереди"
            self._progress(job, cancel, f"Озвучка: {stage}", VOICE_SHARE * float(inner.get("percent") or 0) / 100)
        if audio["status"] == "canceled":
            raise runner.Canceled
        if audio["status"] != "done":
            raise runner.RunFailed(f"Озвучка не удалась: {audio.get('error') or 'без причины'}")
        source = self.audio.audio_path(audio["id"])
        if source is None:
            raise runner.RunFailed("Озвучка готова, но mp3 не найден.")
        target = self._dir(job["id"]) / "voice.mp3"
        shutil.copyfile(source, target)
        self._note(job, f"Озвучка: {job['voice']['engine_label']}, голос {job['voice']['voice']}")
        return target

    def _animate(self, job: dict, spec: AvatarSpec, wav: Path, cancel: threading.Event) -> Path:
        folder = self._dir(job["id"])
        result_dir = folder / "sadtalker"
        shutil.rmtree(result_dir, ignore_errors=True)
        base = VOICE_SHARE if spec.voice else 0.0
        parser = sadtalker.ProgressParser()
        render = sadtalker.RenderSpec(wav, folder / job["face"], spec.size, spec.frame, spec.enhance)
        argv = sadtalker.build_argv(self._config.sadtalker_cmd(), render, result_dir, self._config.weights)

        def on_line(line: str) -> None:
            is_bar = parser.feed(line)
            snap = parser.snapshot()
            percent = base + (100 - base) * snap["percent"] / 100
            self._progress(job, cancel, snap["stage"], percent, step=snap["step"], steps=snap["steps"])
            if not is_bar and line.strip():
                self._note(job, line)

        def on_wait(holder: str) -> None:
            self._progress(job, cancel, f"Ждёт GPU (занят: {holder})", base)

        # GFPGAN и детектор лиц ищут веса в gfpgan/weights от рабочего каталога — запускаем из каталога весов.
        cwd = None if self._config.fake else self._config.weights
        with self._gate.hold(GPU_OWNER, on_wait, cancel):
            self._progress(job, cancel, "Запуск SadTalker", base)
            runner.run(argv, on_line, cancel, cwd=cwd, missing_hint=INSTALL_HINT, kind="avatar")
        if parser.no_face:
            raise runner.RunFailed(NO_FACE)
        result = parser.result if parser.result and parser.result.is_file() else None
        result = result or max(result_dir.glob("*.mp4"), key=lambda p: p.stat().st_mtime, default=None)
        if result is None or result.stat().st_size == 0:
            raise runner.RunFailed("SadTalker завершился без ошибки, но видео не появилось.")
        return result

    def _ffmpeg(self, args: list[str], cancel: threading.Event, what: str) -> None:
        lines: list[str] = []
        argv = [self._config.ffmpeg, "-y", "-loglevel", "error", *args]
        try:
            runner.run(argv, lines.append, cancel, kind="render")
        except runner.RunFailed as exc:
            raise runner.RunFailed(f"ffmpeg ({what}): {(lines or [str(exc)])[-1]}") from exc


def _error_text(reason: str, lines: list[str]) -> str:
    """Причина + последняя значимая строка вывода процесса (если причина не наша, понятная)."""
    if reason == NO_FACE:
        return reason
    for line in reversed(lines):
        text = line.strip()
        if text and not text.startswith("["):
            return f"{reason} Последнее сообщение: {text}"
    return reason
