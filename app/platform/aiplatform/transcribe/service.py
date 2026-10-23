"""Вкладка «Расшифровка»: видео или аудио → текст, Markdown, субтитры. API под /api/transcribe/.

Модели и воркер — «Монтажа» (AIP_MONTAGE_*), данные — <AIP_DATA_DIR>/transcribe/jobs/<id>/.
"""

from __future__ import annotations

from http import HTTPStatus
from urllib.parse import unquote

from aiplatform.core.http import HttpError, Request, bad_request, found, not_found
from aiplatform.montage import asr
from aiplatform.montage import config as montage_config
from aiplatform.transcribe.jobs import (
    ACCEPTED,
    AUDIO_EXT,
    DEFAULT_OPTIONS,
    FILES,
    MAX_TERMS,
    VIDEO_EXT,
    Conflict,
    Transcripts,
    ValidationError,
)

PREFIX = "transcribe"
URL_BASE = f"/api/{PREFIX}"
MAX_BODY = 64 * 1024
MISSING = "Задача не найдена."
SOURCE_MIME = {"mov": "video/quicktime", "webm": "video/webm", "mp3": "audio/mpeg", "wav": "audio/wav",
               "m4a": "audio/mp4", "ogg": "audio/ogg", "flac": "audio/flac"}  # fmt: skip


class TranscribeModule:
    prefix = PREFIX
    label = "Расшифровка"

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.config = montage_config.load(ctx.config)
        self.jobs = Transcripts(self.config, ctx.config.data_dir / PREFIX, ctx.gate, URL_BASE)

    def describe(self) -> dict:
        return {
            "models": asr.public(self.config),
            "default_model": asr.default_id(self.config),
            "languages": [{"id": key, "label": label} for key, (label, _) in asr.LANGUAGES.items()],
            "defaults": DEFAULT_OPTIONS,
            "formats": [ext for _, ext in FILES.values()],
            "accept": [*VIDEO_EXT, *AUDIO_EXT],
            "accepted": ACCEPTED,
            "backend": self.config.backend,
            "max_upload_bytes": self.config.max_upload_bytes,
            "max_terms": MAX_TERMS,
        }

    def problems(self) -> list[str]:
        return list(self.jobs.load_problems)

    def handle(self, req: Request) -> None:
        try:
            return self._route(req)
        except ValidationError as exc:
            raise bad_request(str(exc)) from exc
        except Conflict as exc:
            raise HttpError(HTTPStatus.CONFLICT, str(exc)) from exc

    def _route(self, req: Request) -> None:
        jobs = self.jobs
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["jobs"]):
                return req.send_json({"jobs": jobs.list()})
            case ("POST", ["jobs"]):
                job = jobs.create(req.json(MAX_BODY))
                return req.send_json(job, HTTPStatus.CREATED, {"Location": f"{URL_BASE}/jobs/{job['id']}"})
            case ("GET", ["jobs", jid]):
                return req.send_json(found(jobs.get(jid), MISSING))
            case ("POST", ["jobs", jid]):
                return req.send_json(found(jobs.update(jid, req.json(MAX_BODY)), MISSING))
            case ("POST", ["jobs", jid, "source"]):
                if jobs.get(jid) is None:
                    raise not_found(MISSING)
                limit = self.config.max_upload_bytes
                too_big = f"Файл больше {limit / 2**30:.0f} ГБ (AIP_MONTAGE_MAX_GB)."
                tmp = req.body_to_file(jobs.dir(jid), limit, too_big)
                name = unquote(req.headers.get("X-File-Name", ""))  # UI шлёт имя в percent-кодировке
                return req.send_json(found(jobs.save_source(jid, tmp, name), MISSING), HTTPStatus.CREATED)
            case ("GET", ["jobs", jid, "source"]):
                source = jobs.source_file(jid)
                if source is None:
                    raise not_found("Файла нет.")
                path, ext = source
                return req.send_file(path, SOURCE_MIME.get(ext, "video/mp4"), ranged=True)
            case ("POST", ["jobs", jid, "start"]):
                return req.send_json(found(jobs.start(jid, req.json(MAX_BODY)), MISSING))
            case ("GET", ["jobs", jid, "files", name]) if name in FILES:
                path = jobs.file(jid, name)
                if path is None:
                    raise not_found("Результата ещё нет.")
                ctype, ext = FILES[name]
                attachment = None if req.arg("inline") else f"transcript-{jid}.{ext}"
                return req.send_file(path, ctype, attachment=attachment)
            case ("POST", ["jobs", jid, "cancel"]):
                return req.send_json(found(jobs.cancel(jid), MISSING))
            case ("POST", ["jobs", jid, "delete"]):
                if not jobs.delete(jid):
                    raise not_found(MISSING)
                return req.send_json({"deleted": jid})
        raise not_found()

    def close(self) -> None:
        self.jobs.close()
