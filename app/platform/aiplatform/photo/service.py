"""Вкладка «Фото»: API под /api/photo/ — то же, что у photogen."""

from __future__ import annotations

from http import HTTPStatus

from aiplatform.core import jobs as core_jobs
from aiplatform.core import uploads as uploads_module
from aiplatform.core.http import Request, bad_request, found, handle_uploads, not_found
from aiplatform.photo import config as photo_config
from aiplatform.photo import models, params, upscale
from aiplatform.photo.jobs import INSTALL_HINT, JobManager

PREFIX = "photo"
URL_BASE = f"/api/{PREFIX}"


class PhotoModule:
    prefix = PREFIX
    label = "Фото"

    def __init__(self, ctx) -> None:
        self.config = photo_config.load(ctx.config)
        self.uploads = ctx.uploads
        self.jobs = JobManager(self.config, ctx.uploads, ctx.gate, URL_BASE)

    def describe(self) -> dict:
        available = self.config.models()
        backends = sorted({m.model.backend for m in available})
        return {
            "models": [m.public() for m in available],
            "default_models": {mode: getattr(models.default(available, mode), "id", None) for mode in models.MODES},
            "backend": self.config.backend,
            "runners": {b: self.config.runner_available(b) for b in backends},
            "models_dir": str(self.config.models_dir),
            "install_hint": INSTALL_HINT,
            "upload_limits": uploads_module.limits(),
            "modes": [{"id": key, "label": label} for key, label in params.MODE_LABELS.items()],
            "sizes": [
                {"id": key, "width": w, "height": h, "label": label} for key, (w, h, label) in params.SIZES.items()
            ],
            "default_size": params.DEFAULT_SIZE,
            "work_sides": list(params.WORK_SIDES),
            "default_work_side": params.DEFAULT_WORK_SIDE,
            "grid": params.GRID,
            "max_side": params.MAX_SIDE,
            "styles": [{"id": key, "label": label} for key, (label, _) in params.STYLES.items()],
            "default_style": params.DEFAULT_STYLE,
            "details": [{"id": key, "label": label} for key, label in params.DETAIL_LABELS.items()],
            "default_detail": params.DEFAULT_DETAIL,
            "default_strength": params.DEFAULT_STRENGTH,
            "min_strength": params.MIN_STRENGTH,
            "upscale": {
                "available": self.config.upscale_available(),
                "mode_label": upscale.MODE_LABEL,
                "targets": [
                    {"id": key, "long_side": side, "label": label} for key, (side, label) in upscale.TARGETS.items()
                ],
            },
        }

    def problems(self) -> list[str]:
        if self.config.fake:
            return []
        available = self.config.models()
        out = []
        if not any(m.installed for m in available):
            out.append(f"Фото: ни одна модель не установлена в {self.config.models_dir}. {INSTALL_HINT}")
        for backend in sorted({m.model.backend for m in available}):
            if not self.config.runner_available(backend):
                out.append(f"Фото: пайплайн {backend} не найден. {INSTALL_HINT}")
        return out

    def handle(self, req: Request) -> None:
        if handle_uploads(req, self.uploads, URL_BASE):
            return
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["jobs"]):
                return req.send_json({"jobs": self.jobs.list(*core_jobs.list_args(req))})
            case ("POST", ["jobs"]):
                payload = req.json()
                try:
                    job = self.jobs.create(payload)
                except params.ValidationError as exc:
                    raise bad_request(str(exc)) from exc
                return req.send_json(job, HTTPStatus.CREATED, {"Location": f"/api/photo/jobs/{job['id']}"})
            case ("GET", ["jobs", job_id]):
                return req.send_json(found(self.jobs.get(job_id)))
            case ("POST", ["jobs", job_id, "cancel"]):
                return req.send_json(found(self.jobs.cancel(job_id)))
            case ("POST", ["jobs", job_id, "upscale"]):
                payload = req.json()
                try:
                    job = found(self.jobs.upscale(job_id, payload))
                except params.ValidationError as exc:
                    raise bad_request(str(exc)) from exc
                return req.send_json(job, HTTPStatus.CREATED, {"Location": f"/api/photo/jobs/{job['id']}"})
            case ("GET", ["jobs", job_id, "image"]):
                path = self.jobs.image_path(job_id)
                if path is None:
                    raise not_found("Картинка ещё не готова или задача не найдена.")
                attachment = f"photo-{job_id}.png" if "download" in req.query else None
                return req.send_file(path, "image/png", attachment=attachment)
        raise not_found()

    def close(self) -> None:
        self.jobs.close()
