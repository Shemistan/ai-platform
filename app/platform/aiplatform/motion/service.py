"""Вкладка «Моушн»: агенты придумывают и рисуют анимации к готовому видео. API под /api/motion/."""

from __future__ import annotations

from http import HTTPStatus

from aiplatform.core.http import HttpError, Request, bad_request, found, not_found
from aiplatform.montage import asr
from aiplatform.motion import agents, scenes
from aiplatform.motion import config as motion_config
from aiplatform.motion.projects import (
    DEFAULT_OPTIONS,
    INSTALL_HINT,
    MAX_BRIEF,
    MAX_FEEDBACK,
    SCENE_FILES,
    SIZES,
    Conflict,
    MotionProjects,
    ValidationError,
)

PREFIX = "motion"
URL_BASE = f"/api/{PREFIX}"
MAX_BODY = 64 * 1024
MISSING = "Проект не найден."


class MotionModule:
    prefix = PREFIX
    label = "Моушн"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.config = motion_config.load(ctx.config)
        self.projects = MotionProjects(self.config, ctx.gate, URL_BASE)

    def describe(self) -> dict:
        montage = self.config.montage
        return {
            "models": asr.public(montage),
            "default_model": asr.default_id(montage),
            "sizes": [{"id": key, "label": label} for key, label in SIZES.items()],
            "layouts": [{"id": key, "label": label} for key, label in scenes.LAYOUTS.items()],
            "roles": [{"id": key, "label": label} for key, label in agents.ROLES.items()],
            "defaults": DEFAULT_OPTIONS,
            "backend": montage.backend,
            "parallel": self.config.parallel,
            "llm_available": montage.llm_available(),
            "hyperframes_available": montage.hyperframes_available(),
            "install_hint": INSTALL_HINT,
            "max_upload_bytes": montage.max_upload_bytes,
            "max_feedback": MAX_FEEDBACK,
            "max_brief": MAX_BRIEF,
        }

    def problems(self) -> list[str]:
        problems = list(self.projects.load_problems)
        if not self.config.fake and not self.config.montage.hyperframes_available():
            problems.append(f"Моушн: {INSTALL_HINT}")
        return problems

    def handle(self, req: Request) -> None:
        try:
            return self._route(req)
        except ValidationError as exc:
            raise bad_request(str(exc)) from exc
        except Conflict as exc:
            raise HttpError(HTTPStatus.CONFLICT, str(exc)) from exc

    def _route(self, req: Request) -> None:
        projects = self.projects
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["montage"]):
                return req.send_json({"projects": projects.montage_list()})
            case ("GET", ["projects"]):
                return req.send_json({"projects": projects.list()})
            case ("POST", ["projects"]):
                project = projects.create(req.json(MAX_BODY))
                location = {"Location": f"{URL_BASE}/projects/{project['id']}"}
                return req.send_json(project, HTTPStatus.CREATED, location)
            case ("GET", ["projects", pid]):
                return req.send_json(found(projects.get(pid), MISSING))
            case ("POST", ["projects", pid]):
                return req.send_json(found(projects.update(pid, req.json(MAX_BODY)), MISSING))
            case ("POST", ["projects", pid, "source"]):
                if projects.get(pid) is None:
                    raise not_found(MISSING)
                limit = self.config.montage.max_upload_bytes
                too_big = f"Файл больше {limit / 2**30:.0f} ГБ (AIP_MONTAGE_MAX_GB)."
                tmp = req.body_to_file(projects.dir(pid), limit, too_big)
                return req.send_json(found(projects.save_source(pid, tmp), MISSING), HTTPStatus.CREATED)
            case ("GET", ["projects", pid, "source"]):
                path = projects.source_file(pid)
                if path is None:
                    raise not_found("Видео нет.")
                return req.send_file(path, "video/mp4", ranged=True)
            case ("POST", ["projects", pid, "start"]):
                return req.send_json(found(projects.start(pid), MISSING))
            case ("POST", ["projects", pid, "scenes"]):
                return req.send_json(found(projects.add_scene(pid, req.json(MAX_BODY)), MISSING))
            case ("POST", ["projects", pid, "scenes", sid, "redo"]):
                body = req.json(MAX_BODY)
                text = body.get("text") if isinstance(body, dict) else None
                return req.send_json(found(projects.redo_scene(pid, sid, text), MISSING))
            case ("POST", ["projects", pid, "scenes", sid]):
                body = req.json(MAX_BODY)
                enabled = body.get("enabled") if isinstance(body, dict) else None
                return req.send_json(found(projects.toggle_scene(pid, sid, enabled), MISSING))
            case ("GET", ["projects", pid, "scenes", sid, name]) if name in SCENE_FILES:
                path = projects.scene_file(pid, sid, name)
                if path is None:
                    raise not_found("Файла сцены ещё нет.")
                attachment = f"{pid}-{sid}-{name}" if name in ("scene.mov", "index.html") else None
                return req.send_file(path, SCENE_FILES[name], ranged=name.endswith((".mp4", ".mov")),
                                     attachment=attachment)  # fmt: skip
            case ("GET", ["projects", pid, "package"]):
                path = projects.file(pid, "package.zip")
                if path is None:
                    raise not_found("Пакета ещё нет: ни одной готовой сцены.")
                return req.send_file(path, "application/zip", attachment=f"motion-{pid}.zip")
            case ("POST", ["projects", pid, "cancel"]):
                return req.send_json(found(projects.cancel(pid), MISSING))
            case ("POST", ["projects", pid, "delete"]):
                if not projects.delete(pid):
                    raise not_found(MISSING)
                return req.send_json({"deleted": pid})
        raise not_found()

    def close(self) -> None:
        self.projects.close()
