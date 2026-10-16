"""Вкладка «Аудио»: текст → mp3 через narrator, API под /api/audio/."""

from __future__ import annotations

from http import HTTPStatus

from aiplatform.audio import config as audio_config
from aiplatform.audio import voices
from aiplatform.audio.jobs import (
    INSTALL_HINT,
    MAX_TEXT,
    TEMPO_RANGE,
    AudioJobs,
    RetimeFailed,
    ValidationError,
    parse_tempo,
)
from aiplatform.core.http import HttpError, Request, bad_request, found, not_found

PREFIX = "audio"
URL_BASE = f"/api/{PREFIX}"
MAX_BODY = 4 * MAX_TEXT + 4096  # UTF-8 кириллица — до 2 байт на знак, плюс JSON-экранирование


class AudioModule:
    prefix = PREFIX
    label = "Аудио"

    def __init__(self, ctx) -> None:
        self.config = audio_config.load(ctx.config)
        self.jobs = AudioJobs(self.config, ctx.gate, URL_BASE)

    def describe(self) -> dict:
        return {
            "engines": [e.public() for e in voices.ENGINES.values()],
            "default_engine": self.config.default_engine,
            "speed_range": list(voices.SPEED_RANGE),
            "tempo_range": list(TEMPO_RANGE),
            "max_text": MAX_TEXT,
            "backend": "fake" if self.config.fake else "narrator",
            "runner_available": self.config.runner_available(),
            "install_hint": INSTALL_HINT,
        }

    def problems(self) -> list[str]:
        if self.config.runner_available():
            return []
        return [f"Аудио: команда «{self.config.narrate_cmd[0]}» не найдена. {INSTALL_HINT}"]

    def handle(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("GET", ["jobs"]):
                return req.send_json({"jobs": self.jobs.list()})
            case ("POST", ["jobs"]):
                payload = req.json(MAX_BODY)
                try:
                    job = self.jobs.create(payload)
                except ValidationError as exc:
                    raise bad_request(str(exc)) from exc
                return req.send_json(job, HTTPStatus.CREATED)
            case ("GET", ["jobs", job_id]):
                job = found(self.jobs.get(job_id))
                return req.send_json(job | {"text": self.jobs.text(job_id)})
            case ("POST", ["jobs", job_id, "cancel"]):
                return req.send_json(found(self.jobs.cancel(job_id)))
            case ("GET", ["jobs", job_id, "audio"]):
                try:
                    tempo = parse_tempo(req.arg("speed"))
                    path = self.jobs.audio_path(job_id, tempo)
                except ValidationError as exc:
                    raise bad_request(str(exc)) from exc
                except RetimeFailed as exc:
                    raise HttpError(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc)) from exc
                if path is None:
                    raise not_found("Аудио ещё не готово или задача не найдена.")
                suffix = "" if tempo == 1.0 else f"-x{tempo:g}"
                attachment = f"audio-{job_id}{suffix}.mp3" if "download" in req.query else None
                return req.send_file(path, "audio/mpeg", ranged=True, attachment=attachment)
        raise not_found()

    def close(self) -> None:
        self.jobs.close()
