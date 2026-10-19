"""Вкладка «Монтаж»: своя запись → ролик для YouTube. API под /api/montage/."""

from __future__ import annotations

from http import HTTPStatus

from aiplatform.core.http import HttpError, Request, bad_request, found, not_found
from aiplatform.montage import asr, capabilities, edl, plan, render
from aiplatform.montage import config as montage_config
from aiplatform.montage.projects import (
    DEFAULT_OPTIONS,
    GATES,
    INSTALL_HINT,
    MAX_FEEDBACK,
    MODES,
    ROLES,
    Conflict,
    MontageProjects,
    ValidationError,
)

PREFIX = "montage"
URL_BASE = f"/api/{PREFIX}"
MAX_BODY = 512 * 1024  # план целиком: вырезки и планы по индексам слов
MISSING = "Проект не найден."
FILES = {
    "subtitles.srt": ("subtitles.srt", "application/x-subrip"),
    "edl.json": ("edl.json", "application/json"),
    "source_manifest.json": ("source_manifest.json", "application/json"),
}


def _conflict(exc: Exception) -> HttpError:
    return HttpError(HTTPStatus.CONFLICT, str(exc))


class MontageModule:
    prefix = PREFIX
    label = "Монтаж"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.config = montage_config.load(ctx.config)
        self.projects = MontageProjects(self.config, ctx.gate, URL_BASE)

    def describe(self) -> dict:
        return {
            "models": asr.public(self.config),
            "default_model": asr.default_id(self.config),
            "grades": [{"id": key, "label": label} for key, (label, _) in render.GRADES.items()],
            "layouts": [{"id": key, "label": label} for key, label in edl.LAYOUTS.items()],
            "kinds": [{"id": key, "label": label} for key, label in edl.KINDS.items()],
            "roles": [{"id": key, "label": label} for key, label in ROLES.items()],
            "modes": [{"id": key, "label": label} for key, label in MODES.items()],
            "gates": list(GATES),
            "defaults": DEFAULT_OPTIONS,
            "backend": self.config.backend,
            "llm_available": self.config.llm_available(),
            "hyperframes_available": self.config.hyperframes_available(),
            "install_hint": INSTALL_HINT,
            "max_upload_bytes": self.config.max_upload_bytes,
            "max_feedback": MAX_FEEDBACK,
        }

    def problems(self) -> list[str]:
        problems = list(self.projects.load_problems)
        if not self.config.fake and not any(asr.installed(m, self.config) for m in asr.CATALOG):
            problems.append(f"Монтаж: не установлено ни одной модели расшифровки. {INSTALL_HINT}")
        return problems

    def handle(self, req: Request) -> None:
        try:
            return self._route(req)
        except ValidationError as exc:
            raise bad_request(str(exc)) from exc
        except Conflict as exc:
            raise _conflict(exc) from exc

    def _route(self, req: Request) -> None:
        projects = self.projects
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            # Реестр возможностей (MASTER_SPEC §3): без запуска команд; probe — с настоящими вызовами.
            case ("GET", ["capabilities"]):
                return req.send_json(capabilities.public(self.config))
            case ("POST", ["capabilities", "probe"]):
                return req.send_json(capabilities.public(self.config, probed=True))
            case ("GET", ["rules"]):
                return req.send_json({"base": plan.base_rules(), "lessons": projects.lessons()})
            case ("POST", ["rules"]):
                return req.send_json({"lessons": projects.save_lessons(_field(req.json(MAX_BODY), "lessons"))})
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
            case ("POST", ["projects", pid, "sources", role]):
                if projects.get(pid) is None:
                    raise not_found(MISSING)
                too_big = f"Файл больше {self.config.max_upload_bytes / 2**30:.0f} ГБ (AIP_MONTAGE_MAX_GB)."
                tmp = req.body_to_file(projects.dir(pid) / "sources", self.config.max_upload_bytes, too_big)
                return req.send_json(found(projects.save_source(pid, role, tmp), MISSING), HTTPStatus.CREATED)
            case ("POST", ["projects", pid, "sources", role, "delete"]):
                return req.send_json(found(projects.remove_source(pid, role), MISSING))
            case ("GET", ["projects", pid, "source", role]):
                path = projects.source(pid, role)
                if path is None:
                    raise not_found("Записи нет.")
                return req.send_file(path, "video/mp4", ranged=True)
            case ("POST", ["projects", pid, "start"]):
                return req.send_json(found(projects.start(pid), MISSING))
            case ("POST", ["projects", pid, "replan"]):
                return req.send_json(found(projects.replan(pid), MISSING))
            case ("GET", ["projects", pid, "plan"]):
                return req.send_json(found(projects.plan_view(pid), MISSING))
            case ("POST", ["projects", pid, "plan"]):
                return req.send_json(found(projects.save_plan(pid, req.json(MAX_BODY)), MISSING))
            # Ворота (§1, §6): человек подтверждает план вырезок, раскадровку или экспорт.
            case ("POST", ["projects", pid, "approve"]):
                return req.send_json(found(projects.approve(pid, _field(req.json(MAX_BODY), "gate")), MISSING))
            # Журнал решений (§6, §10): роли, версии промптов, хэши входа и выхода.
            case ("GET", ["projects", pid, "decisions"]):
                return req.send_json({"decisions": found(projects.decisions(pid), MISSING)})
            # Карта времени (§4): сегменты результата в кадрах ↔ исходник в секундах.
            case ("GET", ["projects", pid, "timemap"]):
                return req.send_json(found(projects.timemap_view(pid), MISSING))
            case ("POST", ["projects", pid, "render"]):
                return req.send_json(found(projects.render(pid), MISSING))
            case ("POST", ["projects", pid, "revise"]):
                return req.send_json(found(projects.revise(pid, _field(req.json(MAX_BODY), "text")), MISSING))
            case ("POST", ["projects", pid, "cancel"]):
                return req.send_json(found(projects.cancel(pid), MISSING))
            case ("POST", ["projects", pid, "delete"]):
                if not projects.delete(pid):
                    raise not_found(MISSING)
                return req.send_json({"deleted": pid})
            case ("GET", ["projects", pid, "video"]):
                path = projects.file(pid, "output.mp4")
                if path is None or (projects.get(pid) or {}).get("video_url") is None:
                    raise not_found("Ролик ещё не собран.")
                attachment = f"montage-{pid}.mp4" if "download" in req.query else None
                return req.send_file(path, "video/mp4", ranged=True, attachment=attachment)
            case ("GET", ["projects", pid, name]) if name in FILES:
                filename, mime = FILES[name]
                path = projects.file(pid, filename)
                if path is None:
                    raise not_found("Файла ещё нет.")
                return req.send_file(path, mime, attachment=f"{pid}-{filename}")
        raise not_found()

    def close(self) -> None:
        self.projects.close()


def _field(body: object, key: str) -> object:
    return body.get(key) if isinstance(body, dict) else None
