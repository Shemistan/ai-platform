"""Вкладка «Видео»: API под /api/video/ — то же, что у videogen."""

from __future__ import annotations

from collections.abc import Callable
from http import HTTPStatus

from aiplatform.core import uploads as uploads_module
from aiplatform.core.http import Request, bad_request, found, handle_uploads, not_found
from aiplatform.video import config as video_config
from aiplatform.video import living, models, timeline
from aiplatform.video.jobs import INSTALL_HINT, JobManager

PREFIX = "video"
URL_BASE = f"/api/{PREFIX}"


class VideoModule:
    prefix = PREFIX
    label = "Видео"

    def __init__(self, ctx) -> None:
        self.config = video_config.load(ctx.config)
        self.uploads = ctx.uploads
        self.jobs = JobManager(self.config, ctx.uploads, ctx.gate, URL_BASE)

    def describe(self) -> dict:
        available = self.config.models()
        return {
            "models": [m.public() for m in available],
            "default_model": getattr(models.default(available), "id", None),
            "backend": self.config.backend,
            "runner_available": self.config.fake or self.config.runner_available(),
            "runner_cmd": self.config.ltx_cmd[0],
            "install_hint": INSTALL_HINT,
            "upload_limits": uploads_module.limits(),
            "min_keyframe_gap": timeline.MIN_KEYFRAME_GAP,
            "default_duration_s": timeline.DEFAULT_DURATION_S,
            "presets": [
                {"id": key, "width": w, "height": h, "label": label} for key, (w, h, label) in timeline.PRESETS.items()
            ],
            "default_preset": timeline.DEFAULT_PRESET,
            "living": living.describe(available),
        }

    def problems(self) -> list[str]:
        if self.config.fake:
            return []
        out = []
        if not any(m.installed for m in self.config.models()):
            out.append(f"Видео: модель не установлена в {self.config.models_dir}. {INSTALL_HINT}")
        if not self.config.runner_available():
            out.append(f"Видео: команда «{self.config.ltx_cmd[0]}» не найдена. {INSTALL_HINT}")
        return out

    def handle(self, req: Request) -> None:
        if handle_uploads(req, self.uploads, URL_BASE):
            return
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["jobs"]):
                return req.send_json({"jobs": self.jobs.list()})
            case ("POST", ["jobs"]):
                return self._create(req, self.jobs.create)
            case ("POST", ["living"]):
                return self._create(req, self.jobs.create_living)
            case ("GET", ["jobs", job_id]):
                return req.send_json(found(self.jobs.get(job_id)))
            case ("POST", ["jobs", job_id, "cancel"]):
                return req.send_json(found(self.jobs.cancel(job_id)))
            case ("GET", ["jobs", job_id, "video"]):
                path = self.jobs.video_path(job_id)
                if path is None:
                    raise not_found("Видео ещё не готово или задача не найдена.")
                attachment = f"video-{job_id}.mp4" if "download" in req.query else None
                return req.send_file(path, "video/mp4", ranged=True, attachment=attachment)
        raise not_found()

    @staticmethod
    def _create(req: Request, create: Callable[[object], dict]) -> None:
        payload = req.json()
        try:
            job = create(payload)
        except timeline.ValidationError as exc:
            raise bad_request(str(exc)) from exc
        req.send_json(job, HTTPStatus.CREATED, {"Location": f"/api/video/jobs/{job['id']}"})

    def close(self) -> None:
        self.jobs.close()
