"""Задачи генерации: хранение на диске, очередь, один воркер."""

from __future__ import annotations

import copy
import json
import queue
import random
import re
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path

from aiplatform.core import cleanup, log, runner
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.gate import Gate
from aiplatform.core.http import malformed
from aiplatform.core.uploads import UploadStore
from aiplatform.video import living, ltx, models, timeline, wan
from aiplatform.video.config import VideoConfig
from aiplatform.video.timeline import GenerationSpec, ValidationError

ACTIVE = ("queued", "running")
LOG_TAIL = 200
_ID = re.compile(r"^[0-9a-f]{12}$")
_NOISE = re.compile(r"^\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")  # цвета терминала в выводе запускателя Wan


GPU_OWNER = "Видео"
WAIT_POLL_S = 0.2
INSTALL_HINT = "Поставь модель видео: make models M=ltx-2.3-q4 в app/platform (README, «Установка моделей»)."


class JobManager:
    def __init__(self, config: VideoConfig, uploads: UploadStore, gate: Gate, url_base: str = "/api/video") -> None:
        self._config = config
        self._uploads = uploads
        self._gate = gate
        self._url = url_base
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._specs: dict[str, GenerationSpec] = {}
        self._models: dict[str, models.Available] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        config.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load()
        self._worker = threading.Thread(target=self._work, name="video-worker", daemon=True)
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
            # Лог отдаётся только в карточке одной задачи: список опрашивается раз в секунду.
            return [{k: v for k, v in self._view(j).items() if k != "log"} for j in jobs]

    def video_path(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "done":
                return None
        path = self._dir(job_id) / "output.mp4"
        return path if path.is_file() else None

    def _view(self, job: dict) -> dict:
        view = copy.deepcopy(job)
        if job["status"] == "queued":
            ahead = [
                j for j in self._jobs.values()
                if j["status"] in ACTIVE and j["created_at"] < job["created_at"]
            ]  # fmt: skip
            view["queue_position"] = len(ahead)
        end = job["finished_at"] or time.time()
        view["elapsed_s"] = round(end - job["started_at"], 1) if job["started_at"] else 0.0
        view["video_url"] = f"{self._url}/jobs/{job['id']}/video" if job["status"] == "done" else None
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

    # --- запись ---

    def pick(self, model_id: object) -> models.Available:
        """Модель по id из запроса; пусто — модель по умолчанию."""
        if model_id is not None and not isinstance(model_id, str):
            raise ValidationError(malformed("model должен быть строкой — id модели."))
        available = self._config.models()
        if not model_id:
            model = models.default(available)
            if model is None:
                raise ValidationError("Все модели видео выключены — включи модель во вкладке «Настройки».")
        else:
            model = next((m for m in available if m.id == model_id), None)
            if model is None:
                raise ValidationError(models.unknown(model_id))
        if not model.installed:
            raise ValidationError(f"Модель {model.label} не установлена: запусти make models M={model.id}.")
        return model

    def create(self, payload: object) -> dict:
        """Запрос из API: ограничения на длительность и кадры зависят от модели."""
        model = self.pick(payload.get("model") if isinstance(payload, dict) else None)
        return self.submit(timeline.build_spec(payload, model.limits), model)

    def create_living(self, payload: object) -> dict:
        """Живое фото: из запроса — снимок, модель, ориентация и качество; остальное сервис выбирает сам."""
        model = living.pick_model(self._config.models(), payload.get("model") if isinstance(payload, dict) else None)
        return self.submit(living.build_spec(payload, model), model)

    def submit(self, spec: GenerationSpec, model: models.Available | None = None) -> dict:
        model = model or self.pick(spec.model)
        for n, keyframe in enumerate(spec.keyframes, start=1):
            if self._uploads.path(keyframe.upload_id) is None:
                raise ValidationError(f"Кадр {n}: картинка не найдена, загрузи её заново.")
        if spec.seed < 0:
            spec = replace(spec, seed=random.randrange(2**31))
        job_id = uuid.uuid4().hex[:12]
        job = {
            "id": job_id,
            "kind": spec.kind,
            "status": "queued",
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "model": model.id,
            "model_label": model.label,
            "backend": self._config.backend,
            "prompt": spec.prompt,
            "duration_s": spec.duration_s,
            "preset": spec.preset,
            "width": spec.width,
            "height": spec.height,
            "num_frames": spec.num_frames,
            "fps": spec.fps,
            "seed": spec.seed,
            "audio": spec.audio,
            "frames": [
                {
                    "upload_id": k.upload_id,
                    "frame_idx": k.frame_idx,
                    "time_s": round(k.frame_idx / spec.fps, 2),
                    "url": f"{self._url}/uploads/{k.upload_id}",
                }
                for k in spec.keyframes
            ],
            "progress": {"stage": "В очереди", "step": None, "steps": None, "percent": 0.0},
            "error": None,
            "log": [],
        }
        with self._lock:
            self._jobs[job_id] = job
            self._cancels[job_id] = threading.Event()
            self._specs[job_id] = spec
            self._models[job_id] = model
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
                self._finish(job, "failed", error="Сервис был перезапущен во время генерации.")
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
                model = self._models.pop(job_id)
                cancel = self._cancels[job_id]
                if job["status"] != "queued":  # отменили, пока ждала в очереди
                    self._cancels.pop(job_id, None)
                    continue
                job["status"] = "running"
                job["started_at"] = time.time()
                job["progress"]["stage"] = "Запуск"
                self._save(job)
            self._run(job, spec, model, cancel)
            with self._lock:
                self._cancels.pop(job_id, None)

    def _run(self, job: dict, spec: GenerationSpec, model: models.Available, cancel: threading.Event) -> None:
        output = self._dir(job["id"]) / "output.mp4"
        parser = wan.ProgressParser() if model.pipeline == "wan" else ltx.ProgressParser()

        def on_line(line: str) -> None:
            line = _ANSI.sub("", line)
            is_bar = parser.feed(line)
            with self._lock:
                if not cancel.is_set():
                    job["progress"] = parser.snapshot()
                if not is_bar and not _NOISE.match(line):
                    job["log"].append(line.rstrip())
                    del job["log"][:-LOG_TAIL]

        try:
            images = [self._uploads.path(k.upload_id) for k in spec.keyframes]
            if any(p is None for p in images):
                raise runner.RunFailed("Картинка кадра удалена с диска до начала генерации.")
            if model.pipeline == "wan":
                argv = wan.build_argv(
                    self._config.wan_cmd, spec, output, model=model.path, steps=self._config.wan_steps
                )
            else:
                argv = ltx.build_argv(
                    self._config.ltx_cmd, spec, images, output,
                    model=model.path, gemma=self._config.gemma(), low_ram=self._config.low_ram,
                )  # fmt: skip

            def on_wait(holder: str) -> None:
                with self._lock:
                    job["progress"]["stage"] = f"Ждёт GPU (занят: {holder})"
                    self._save(job)

            with self._gate.hold(GPU_OWNER, on_wait, cancel):
                with self._lock:
                    job["progress"]["stage"] = "Запуск"
                    job["started_at"] = time.time()  # время генерации — без ожидания GPU
                runner.run(argv, on_line, cancel, missing_hint=INSTALL_HINT, kind="video")
            if not output.is_file() or output.stat().st_size == 0:
                raise runner.RunFailed("Генерация завершилась без ошибки, но файл видео не появился.")
        except runner.Canceled:
            output.unlink(missing_ok=True)
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


def _error_text(reason: str, log: list[str]) -> str:
    """Причина + последняя значимая строка вывода процесса."""
    for line in reversed(log):
        text = line.strip()
        if text and not text.startswith("["):
            return f"{reason} Последнее сообщение: {text}"
    return reason
