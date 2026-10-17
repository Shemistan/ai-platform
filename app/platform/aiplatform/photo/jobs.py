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
from dataclasses import asdict, replace
from pathlib import Path

from aiplatform.core import cleanup, runner
from aiplatform.core.gate import Gate
from aiplatform.core.uploads import UploadStore
from aiplatform.photo import flux2, mflux, models, params, upscale
from aiplatform.photo.config import PhotoConfig
from aiplatform.photo.params import MAX_SEED, GenerationSpec, ValidationError
from aiplatform.photo.upscale import UpscaleSpec

ACTIVE = ("queued", "running")
INSTALL_HINT = "Поставь модели фото: ./setup-model.sh в 11_photo_generator/app/photogen (см. README платформы)."
LOG_TAIL = 200
OUTPUT = "output.png"
_ID = re.compile(r"^[0-9a-f]{12}$")
_NOISE = re.compile(r"^\s*$")


GPU_OWNER = "Фото"
WAIT_POLL_S = 0.2


class JobManager:
    def __init__(self, config: PhotoConfig, uploads: UploadStore, gate: Gate, url_base: str = "/api/photo") -> None:
        self._config = config
        self._uploads = uploads
        self._gate = gate
        self._url = url_base
        self._lock = threading.Lock()
        self._jobs: dict[str, dict] = {}
        self._cancels: dict[str, threading.Event] = {}
        self._specs: dict[str, GenerationSpec | UpscaleSpec] = {}
        self._models: dict[str, models.Available | None] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        config.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._load()
        self._worker = threading.Thread(target=self._work, name="photo-worker", daemon=True)
        self._worker.start()

    # --- чтение ---

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return self._view(job) if job else None

    def list(self) -> list[dict]:
        with self._lock:
            jobs = sorted(self._jobs.values(), key=lambda j: j["created_at"], reverse=True)
            # Лог отдаётся только в карточке одной задачи: список опрашивается раз в секунду.
            return [{k: v for k, v in self._view(j).items() if k != "log"} for j in jobs]

    def forget(self, job_id: str) -> int:
        """Уборка: удалить завершённую задачу из истории и с диска; идущую — нельзя. Возвращает байты."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job["status"] in ACTIVE:
                return 0
            del self._jobs[job_id]
        return cleanup.remove_tree(self._dir(job_id))

    def image_path(self, job_id: str) -> Path | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job or job["status"] != "done":
                return None
        path = self._dir(job_id) / OUTPUT
        return path if path.is_file() else None

    def _view(self, job: dict) -> dict:
        view = copy.deepcopy(job)
        if job["status"] == "queued":
            ahead = [
                j for j in self._jobs.values()
                if j["status"] in ACTIVE and j["created_at"] < job["created_at"]
            ]  # fmt: skip
            view["queue_position"] = len(ahead)
        # Время генерации — от старта процесса, ожидание в очереди в него не входит.
        end = job["finished_at"] or time.time()
        view["elapsed_s"] = round(end - job["started_at"], 1) if job["started_at"] else 0.0
        view["image_url"] = f"{self._url}/jobs/{job['id']}/image" if job["status"] == "done" else None
        done = job["status"] == "done" and self._config.upscale_available()
        view["upscale_targets"] = upscale.targets_for(job["width"], job["height"]) if done else []
        return view

    # --- для сценариев ---

    def create(self, payload: object) -> dict:
        """Задача из JSON запроса, как с вкладки: проверка параметров и постановка в очередь."""
        spec = params.build_spec(payload, self._config.models(), self._uploads.size)
        return self.submit(spec)

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

    def submit(self, spec: GenerationSpec) -> dict:
        for upload_id, what in ((spec.source, "Исходное фото"), (spec.mask, "Маска")):
            if upload_id and self._uploads.path(upload_id) is None:
                raise ValidationError(f"{what}: файл не найден, загрузи заново.")
        model = next((m for m in self._config.models() if m.id == spec.model), None)
        if model is None or not model.installed:
            raise ValidationError("Модель недоступна — обнови страницу.")
        if spec.seed < 0:
            spec = replace(spec, seed=random.randrange(MAX_SEED + 1))
        job = {
            "id": uuid.uuid4().hex[:12],
            "model": model.id,
            "model_label": model.label,
            "mode": spec.mode,
            "prompt": spec.prompt,
            "negative_prompt": spec.negative_prompt,
            "style": spec.style,
            "detail": spec.detail,
            "width": spec.width,
            "height": spec.height,
            "steps": spec.steps,
            "preset": spec.preset,
            "guidance": spec.guidance if model.model.guidance else None,
            "strength": spec.strength,
            "seed": spec.seed,
            "source_url": f"{self._url}/uploads/{spec.source}" if spec.source else None,
            "mask_url": f"{self._url}/uploads/{spec.mask}" if spec.mask else None,
            "padding": asdict(spec.padding) if spec.padding.any() else None,
        }
        return self._enqueue(job, spec, model)

    def upscale(self, parent_id: str, payload: object) -> dict | None:
        """Увеличить готовую картинку задачи parent_id. None — нет такой задачи."""
        with self._lock:
            parent = self._jobs.get(parent_id)
            if parent is None:
                return None
            parent = copy.deepcopy(parent)
        if self.image_path(parent_id) is None:
            raise ValidationError("Увеличить можно только готовую картинку.")
        if not self._config.upscale_available():
            raise ValidationError(f"Апскейлер {upscale.COMMAND} не найден. {INSTALL_HINT}")
        spec = upscale.build_spec(payload, parent_id, parent["width"], parent["height"], parent["seed"])
        job = {
            "id": uuid.uuid4().hex[:12],
            "model": upscale.MODEL_ID,
            "model_label": upscale.MODEL_LABEL,
            "mode": upscale.MODE,
            "prompt": parent["prompt"],
            "negative_prompt": "",
            "style": None,
            "detail": None,
            "width": spec.width,
            "height": spec.height,
            "steps": 1,
            "preset": None,
            "guidance": None,
            "strength": None,
            "seed": spec.seed,
            "parent_id": parent_id,
            "source_size": [parent["width"], parent["height"]],
            "source_url": f"{self._url}/jobs/{parent_id}/image",
            "mask_url": None,
            "padding": None,
        }
        return self._enqueue(job, spec, None)

    def _enqueue(self, job: dict, spec: GenerationSpec | UpscaleSpec, model: models.Available | None) -> dict:
        job.update(
            status="queued",
            created_at=time.time(),
            started_at=None,
            finished_at=None,
            backend=self._config.backend,
            progress={"stage": "В очереди", "step": None, "steps": None, "percent": 0.0},
            error=None,
            log=[],
        )
        job_id = job["id"]
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

    def _command(
        self, spec: GenerationSpec | UpscaleSpec, model: models.Available | None, output: Path
    ) -> tuple[list[str], object]:
        if isinstance(spec, UpscaleSpec):
            source = self._dir(spec.parent) / OUTPUT
            if not source.is_file():
                raise runner.RunFailed("Исходная картинка удалена с диска до начала увеличения.")
            argv = upscale.build_argv(
                self._config.mflux_cmd(upscale.COMMAND), spec, source, output, low_ram=self._config.low_ram
            )
            return argv, mflux.ProgressParser()
        source = self._uploads.path(spec.source) if spec.source else None
        mask = self._uploads.path(spec.mask) if spec.mask else None
        if (spec.source and source is None) or (spec.mask and mask is None):
            raise runner.RunFailed("Исходное фото или маска удалены с диска до начала генерации.")
        low_ram = self._config.low_ram
        if model.model.backend == models.FLUX2:
            argv = flux2.build_argv(
                self._config.flux2(), model.model, spec, output,
                models_dir=model.path, source=source, mask=mask, low_ram=low_ram,
            )  # fmt: skip
            return argv, flux2.ProgressParser()
        argv = mflux.build_argv(
            self._config.mflux_cmd(model.model.command), model.model, spec, output,
            weights=model.path, source=source, low_ram=low_ram,
        )  # fmt: skip
        return argv, mflux.ProgressParser()

    def _env(self) -> dict[str, str]:
        """Всё, что пайплайн решит докачать сам, должно лечь рядом с весами, а не на системный диск."""
        root = self._config.models_dir
        if self._config.fake or root is None:
            return {}
        cache = root / models.CACHE_DIR
        return {"HF_HOME": str(cache / "hf"), "MFLUX_CACHE_DIR": str(cache / "mflux")}

    def _run(
        self, job: dict, spec: GenerationSpec | UpscaleSpec, model: models.Available | None, cancel: threading.Event
    ) -> None:
        output = self._dir(job["id"]) / OUTPUT

        try:
            argv, parser = self._command(spec, model, output)

            def on_line(line: str) -> None:
                is_bar = parser.feed(line)
                with self._lock:
                    if not cancel.is_set():
                        job["progress"] = parser.snapshot()
                    if not is_bar and not _NOISE.match(line):
                        job["log"].append(line.rstrip())
                        del job["log"][:-LOG_TAIL]

            def on_wait(holder: str) -> None:
                with self._lock:
                    job["progress"]["stage"] = f"Ждёт GPU (занят: {holder})"
                    self._save(job)

            with self._gate.hold(GPU_OWNER, on_wait, cancel):
                with self._lock:
                    job["progress"]["stage"] = "Запуск"
                    job["started_at"] = time.time()  # время генерации — без ожидания GPU
                runner.run(argv, on_line, cancel, env=self._env(), missing_hint=INSTALL_HINT)
            if not output.is_file() or output.stat().st_size == 0:
                raise runner.RunFailed("Генерация завершилась без ошибки, но файл картинки не появился.")
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
                self._finish(job, "done")


def _error_text(reason: str, log: list[str]) -> str:
    """Причина + последняя значимая строка вывода процесса."""
    for line in reversed(log):
        text = line.strip()
        if text and not text.startswith("["):
            return f"{reason} Последнее сообщение: {text}"
    return reason
