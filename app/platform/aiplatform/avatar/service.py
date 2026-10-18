"""Вкладка «Диктор»: персонаж по фото говорит озвученный текст. API под /api/avatar/."""

from __future__ import annotations

from http import HTTPStatus

from aiplatform.audio.jobs import MAX_TEXT
from aiplatform.avatar import config as avatar_config
from aiplatform.avatar import models, sadtalker
from aiplatform.avatar.characters import CONSENT_FORMS, KINDS, CharacterError, CharacterStore, consent_of
from aiplatform.avatar.jobs import INSTALL_HINT, AvatarJobs, ValidationError
from aiplatform.core import audit
from aiplatform.core import jobs as core_jobs
from aiplatform.core import uploads as uploads_module
from aiplatform.core.http import Request, bad_request, found, handle_uploads, not_found
from aiplatform.core.uploads import AUDIO, IMAGE, MIME

PREFIX = "avatar"
URL_BASE = f"/api/{PREFIX}"
MAX_BODY = 4 * MAX_TEXT + 4096  # как у «Аудио»: текст для озвучки


class AvatarModule:
    prefix = PREFIX
    label = "Диктор"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.config = avatar_config.load(ctx.config)
        self.uploads = ctx.uploads
        self.characters = CharacterStore(self.config.characters_dir, URL_BASE)
        self.jobs = AvatarJobs(self.config, ctx.uploads, self.characters, ctx.gate, URL_BASE)
        self.audit = audit.AuditLog(ctx.config.data_dir / audit.FILE)  # согласие на лицо — в журнал комплаенса

    def bind(self) -> None:
        """Озвучка — очередью вкладки «Аудио»: она уже создана, когда зовётся bind."""
        audio = self.ctx.modules.get("audio")
        if audio is not None:
            self.jobs.audio = audio.jobs
            self.jobs.audio_config = audio.config

    def describe(self) -> dict:
        return {
            "models": models.public(self.config),
            "default_model": models.DEFAULT_ID,
            "frames": [{"id": key, "label": label} for key, label in sadtalker.FRAMES.items()],
            "default_frame": sadtalker.DEFAULT_FRAME,
            "kinds": [{"id": key, "label": label} for key, label in KINDS.items()],
            "consent_forms": [{"id": key, "label": label} for key, label in CONSENT_FORMS.items()],
            "backend": self.config.backend,
            "runner_available": self.config.runner_available(),
            "install_hint": INSTALL_HINT,
            "audio_available": self.jobs.audio is not None,
            "max_text": MAX_TEXT,
            "upload_limits": uploads_module.limits(),
        }

    def problems(self) -> list[str]:
        lack = models.missing(self.config)
        problems = list(self.jobs.load_problems)
        if lack:
            problems.append(f"Диктор: у SadTalker нет {' и '.join(lack)}. {INSTALL_HINT}")
        return problems

    def handle(self, req: Request) -> None:
        if handle_uploads(req, self.uploads, URL_BASE, (IMAGE, AUDIO)):
            return None
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["characters"]):
                return req.send_json({"characters": self.characters.list()})
            case ("POST", ["characters"]):
                return req.send_json(self.add_character(req.json()), HTTPStatus.CREATED)
            case ("GET", ["characters", char_id, "image"]):
                path = self.characters.image(char_id)
                if path is None:
                    raise not_found("Персонаж не найден.")
                return req.send_file(path, MIME[path.suffix[1:]], cache="private, max-age=86400")
            case ("POST", ["characters", char_id, "rename"]):
                try:
                    return req.send_json(found(self.characters.rename(char_id, req.json())))
                except CharacterError as exc:
                    raise bad_request(str(exc)) from exc
            case ("POST", ["characters", char_id, "delete"]):
                if not self.characters.delete(char_id):
                    raise not_found("Персонаж не найден.")
                return req.send_json({"deleted": char_id})
            case ("GET", ["jobs"]):
                return req.send_json({"jobs": self.jobs.list(*core_jobs.list_args(req))})
            case ("POST", ["jobs"]):
                try:
                    job = self.jobs.create(req.json(MAX_BODY))
                except ValidationError as exc:
                    raise bad_request(str(exc)) from exc
                return req.send_json(job, HTTPStatus.CREATED, {"Location": f"{URL_BASE}/jobs/{job['id']}"})
            case ("GET", ["jobs", job_id]):
                return req.send_json(found(self.jobs.get(job_id)))
            case ("POST", ["jobs", job_id, "cancel"]):
                return req.send_json(found(self.jobs.cancel(job_id)))
            case ("GET", ["jobs", job_id, "face"]):
                path = self.jobs.face_path(job_id)
                if path is None:
                    raise not_found("Картинки задачи нет.")
                return req.send_file(path, MIME[path.suffix[1:]], cache="private, max-age=86400")
            case ("GET", ["jobs", job_id, "video"]):
                path = self.jobs.video_path(job_id)
                if path is None:
                    raise not_found("Видео ещё не готово или задача не найдена.")
                attachment = f"avatar-{job_id}.mp4" if "download" in req.query else None
                return req.send_file(path, "video/mp4", ranged=True, attachment=attachment)
        raise not_found()

    def add_character(self, payload: object) -> dict:
        upload = payload.get("upload_id") if isinstance(payload, dict) else None
        source = self.uploads.path(upload)
        if source is not None and source.suffix[1:] not in uploads_module.IMAGE_MIME:
            raise bad_request("Картинка персонажа — PNG, JPEG или WebP.")
        digest = audit.sha256_file(source) if source else None  # до переезда файла в каталог персонажа
        try:
            item = self.characters.create(payload, source)
        except CharacterError as exc:
            raise bad_request(str(exc)) from exc
        if item["kind"] == "photo":
            consent = consent_of(payload["consent"])  # уже проверено в create — здесь только нормализация
            self.audit.write(
                "likeness_consent",
                character=item["id"],
                name=item["name"],
                owner=consent["owner"],
                form=consent["form"],
                image_sha256=digest,
            )
        return item

    def close(self) -> None:
        self.jobs.close()
