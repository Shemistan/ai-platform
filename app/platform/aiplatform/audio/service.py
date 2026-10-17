"""Вкладка «Аудио»: текст → mp3 через движок narrator, API под /api/audio/.

Кроме очереди озвучки: как модель прочитает текст (предпросмотр), разбор
md-файла на статьи, тексты из библиотеки статей и дайджестов, образцы
голосов-клонов. Что умеют движки и какие голоса записаны, говорит мост
`narrate bridge engines`; не ответил — берётся встроенный список voices.py.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path

from aiplatform.audio import config as audio_config
from aiplatform.audio import voices
from aiplatform.audio.jobs import (
    MAX_TEXT,
    STEPS_RANGE,
    TEMPO_RANGE,
    AudioJobs,
    RetimeFailed,
    ValidationError,
    build_spec,
    parse_tempo,
)
from aiplatform.core import audit
from aiplatform.core import tasks as tasks_module
from aiplatform.core import uploads as uploads_module
from aiplatform.core.http import HttpError, Request, bad_request, found, handle_uploads, not_found
from aiplatform.core.narrator import INSTALL_HINT, NarratorError, http_error
from aiplatform.core.uploads import AUDIO

PREFIX = "audio"
URL_BASE = f"/api/{PREFIX}"
MAX_BODY = 4 * MAX_TEXT + 4096  # UTF-8 кириллица — до 2 байт на знак, плюс JSON-экранирование
MAX_FILE_BODY = 16 * 1024 * 1024  # md-файл с несколькими статьями
MAX_BATCH = 100
ENGINES_TTL_S = 30  # движки и голоса меняются редко: после записи образца кэш сбрасывается
VOICE_TASK = "voice"


class AudioModule:
    prefix = PREFIX
    label = "Аудио"

    def __init__(self, ctx) -> None:
        self.config = replace(audio_config.load(ctx.config), installed=self._installed)
        self.narrator = self.config.narrator
        self.uploads = ctx.uploads
        self.jobs = AudioJobs(self.config, ctx.gate, URL_BASE)
        self.tasks = tasks_module.Tasks(self.config.data_dir / "tasks", ctx.gate)
        self.audit = audit.AuditLog(ctx.config.data_dir / audit.FILE)  # запись голоса — в журнал комплаенса
        self._engines_lock = threading.Lock()
        self._engines: tuple[float, dict] | None = None

    # --- движки ---

    def engines(self, fresh: bool = False) -> dict:
        """Ответ моста engines с кэшем; ошибка моста — поле error, список — встроенный."""
        with self._engines_lock:
            if not fresh and self._engines and time.time() - self._engines[0] < ENGINES_TTL_S:
                return self._engines[1]
        try:
            info = self.narrator.bridge("engines")
        except NarratorError as exc:
            info = {"error": str(exc)}
        with self._engines_lock:
            self._engines = (time.time(), info)
        return info

    def describe(self) -> dict:
        info = self.engines()
        live = {e["id"]: e for e in info.get("engines") or []}
        engines = []
        for engine in self.config.engines().values():
            item = engine.public() | {"steps": engine.tunable, "problem": None, "default_steps": None}
            found_engine = live.get(engine.id)
            if found_engine:
                # Голос-клон без записанного образца движок не предлагает.
                item["voices"] = [v for v in engine.voices if v in found_engine["voices"]] or item["voices"]
                item["problem"] = found_engine.get("problem")
                item["default_steps"] = found_engine.get("default_steps")
                item["default_speed"] = found_engine.get("default_speed")
            engines.append(item)
        return {
            "engines": engines,
            "default_engine": self.config.default_engine,
            "speed_range": list(voices.SPEED_RANGE),
            "steps_range": list(STEPS_RANGE),
            "tempo_range": list(TEMPO_RANGE),
            "max_text": MAX_TEXT,
            "max_batch": MAX_BATCH,
            "verify_available": bool(info.get("verify_available")),
            "backend": "fake" if self.config.fake else "narrator",
            "runner_available": self.config.runner_available(),
            "engines_error": info.get("error"),
            "models_hint": info.get("models_hint"),
            "install_hint": INSTALL_HINT,
            "upload_limits": uploads_module.limits(),
        }

    def _installed(self) -> set[str] | None:
        """Движки, у которых мост не нашёл проблем (веса на месте); None — мост не ответил."""
        info = self.engines()
        if info.get("error") or not isinstance(info.get("engines"), list):
            return None
        return {e["id"] for e in info["engines"] if isinstance(e, dict) and not e.get("problem")}

    def problems(self) -> list[str]:
        problem = self.narrator.check()  # нет команды, старый движок, другая версия моста
        if problem:
            return [problem]
        info = self.engines()
        live = {e.get("id"): e for e in info.get("engines") or [] if isinstance(e, dict)}
        engine_id = self.config.default_engine
        problem = (live.get(engine_id) or {}).get("problem")
        if info.get("error") or not problem:
            return []
        engine = voices.ENGINES.get(engine_id)
        label = engine.label if engine else engine_id
        hint = voices.models_command(engine_id)
        where = info.get("models_hint")
        tail = f" Папка моделей: {where}." if where and not info.get("models_dir_exists", True) else ""
        return [f"Аудио: у движка по умолчанию {label} нет весов ({problem}). Поставь: {hint}.{tail}"]

    # --- маршруты ---

    def handle(self, req: Request) -> None:
        if handle_uploads(req, self.uploads, URL_BASE, (AUDIO,)):
            return None
        match (req.method, req.sub):
            case ("GET", ["config"]):
                return req.send_json(self.describe())
            case ("POST", ["preview"]):
                text = _text_field(req.json(MAX_BODY))
                return req.send_json(self._bridge("preview", stdin=text))
            case ("POST", ["split"]):
                text = _text_field(req.json(MAX_FILE_BODY))
                return req.send_json(self._bridge("split", stdin=text))
            case ("GET", ["library"]):
                return req.send_json(self.library())
            case ("GET", ["library", item_id]):
                card = self._bridge("article", item_id)
                return req.send_json({"id": item_id, "title": card.get("title", ""), "text": card.get("script", "")})
            case ("GET", ["jobs"]):
                return req.send_json({"jobs": self.jobs.list()})
            case ("POST", ["jobs"]):
                payload = req.json(MAX_BODY)
                try:
                    job = self.jobs.create(payload)
                except ValidationError as exc:
                    raise bad_request(str(exc)) from exc
                return req.send_json(job, HTTPStatus.CREATED, {"Location": f"/api/audio/jobs/{job['id']}"})
            case ("POST", ["jobs", "batch"]):
                return req.send_json(self.batch(req.json(MAX_FILE_BODY)), HTTPStatus.CREATED)
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
            case ("GET", ["voices"]):
                return req.send_json(self.voices())
            case ("GET", ["voices", stem, "sample"]):
                path = self._sample(stem)
                return req.send_file(path, "audio/wav", ranged=True)
            case ("POST", ["voices", stem]):
                return req.send_json(self.record_voice(stem, req.json()), HTTPStatus.ACCEPTED)
            case ("GET", ["tasks", task_id]):
                return req.send_json(found(self.tasks.get(task_id)))
        raise not_found()

    # --- действия ---

    def _bridge(self, *args: str, stdin: str | dict | None = None) -> dict:
        try:
            return self.narrator.bridge(*args, stdin=stdin)
        except NarratorError as exc:
            raise http_error(exc) from exc

    def library(self) -> dict:
        """Что можно взять в поле текста: статьи с текстом для озвучки и дайджесты."""
        data = self._bridge("library")
        items = [{"id": d["id"], "title": d["title"], "added": d["added"], "kind": "digest"} for d in data["digests"]]
        items += [
            {"id": a["id"], "title": a["title"], "added": a["added"], "kind": "article"}
            for a in data["articles"]
            if a["has_script"]
        ]
        items.sort(key=lambda i: i["added"], reverse=True)
        return {"items": items}

    def batch(self, payload: object) -> dict:
        """Статьи одного файла — в очередь по порядку, каждая своей задачей с общими настройками."""
        if not isinstance(payload, dict) or not isinstance(payload.get("articles"), list):
            raise bad_request("Нужен список статей.")
        articles = payload["articles"]
        if not articles:
            raise bad_request("Список статей пуст.")
        if len(articles) > MAX_BATCH:
            raise bad_request(f"За раз — не больше {MAX_BATCH} статей.")
        settings = {k: payload.get(k) for k in ("engine", "voice", "speed", "steps", "verify")}
        try:
            specs = [
                build_spec(
                    settings | {"text": a.get("text"), "title": a.get("title")},
                    self.config.default_engine,
                    self.config.engines(),
                )
                for a in articles
                if isinstance(a, dict)
            ]
        except ValidationError as exc:
            raise bad_request(str(exc)) from exc
        return {"jobs": [self.jobs.submit(spec) for spec in specs]}

    def voices(self) -> dict:
        data = self._bridge("voices")
        for v in data["voices"]:
            v["sample_url"] = f"{URL_BASE}/voices/{v['stem']}/sample" if v.pop("sample", None) else None
        task = self.tasks.busy(VOICE_TASK)
        data["task"] = task
        return data

    def _sample(self, stem: str) -> Path:
        for v in self._bridge("voices")["voices"]:
            if v["stem"] == stem and v.get("sample"):
                path = Path(v["sample"])
                if path.is_file():
                    return path
        raise not_found("Образца этого голоса нет.")

    def record_voice(self, stem: str, payload: object) -> dict:
        """Перезаписать образец: из Gemini TTS (Charon) или из загруженной записи и её текста."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        known = {v["stem"]: v for v in self._bridge("voices")["voices"]}
        voice = known.get(stem)
        if voice is None or not voice["record"]:
            raise bad_request("Этот образец не перезаписывается.")
        if self.tasks.busy(VOICE_TASK):
            raise HttpError(HTTPStatus.CONFLICT, "Уже записывается образец — дождись конца.")
        argv = self.narrator.argv("voice", stem)
        text = payload.get("text")
        if isinstance(text, str) and text.strip():
            argv += ["--text", " ".join(text.split())]
        upload = payload.get("upload")
        if upload:
            path = self.uploads.path(upload)
            if path is None:
                raise bad_request("Загрузи запись образца ещё раз — файл не найден.")
            argv += ["--from", str(path)]
            if not (isinstance(text, str) and text.strip()):
                raise bad_request("Напиши, что сказано в записи, слово в слово.")
        elif voice["record"] != "gemini":
            raise bad_request("Для этого голоса нужна своя запись и её текст.")

        def done(task: dict) -> None:
            self.engines(fresh=True)  # появился образец — у клонов новый голос
            self.audit.write(
                "voice_record",
                who=who,
                voice=voice["voice"],
                stem=stem,
                source="upload" if upload else "gemini",
                status=task.get("status"),
                text_sha256=audit.sha256_text(text) if isinstance(text, str) and text.strip() else None,
                upload_sha256=upload_sha256,
            )

        who = audit.actor()
        upload_sha256 = audit.sha256_file(path) if upload else None  # до задачи: файл загрузки она заберёт

        spec = tasks_module.Spec(
            kind=VOICE_TASK,
            title=f"Образец голоса {voice['voice']}",
            argv=argv,
            cwd=self.narrator.cwd,
            env=self.narrator.env,
            target=stem,
            hint=INSTALL_HINT,
            on_done=done,
        )
        return self.tasks.start(spec)

    def close(self) -> None:
        self.tasks.close()
        self.jobs.close()


def _text_field(payload: object) -> str:
    text = payload.get("text") if isinstance(payload, dict) else None
    if not isinstance(text, str):
        raise bad_request("Нужен текст.")
    return text
