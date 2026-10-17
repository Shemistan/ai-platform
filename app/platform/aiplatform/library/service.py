"""Вкладка «Статьи»: библиотека статей для прослушивания, API под /api/library/.

Статья — каталог в library/ движка: исходный текст (source.md), текст для
озвучки (script.txt), карточка с описанием (meta.json), mp3 по движкам,
отчёт проверки (check.md) и отметка, ушла ли озвучка в Telegram. Читает
и правит их мост `narrate bridge`; озвучка, проверка, рассылка и агент,
который готовит текст, — фоновые задачи с логом: команды движка.
"""

from __future__ import annotations

import re
from http import HTTPStatus
from pathlib import Path
from urllib.parse import quote

from aiplatform.audio import voices
from aiplatform.config import Config
from aiplatform.core import narrator as narrator_module
from aiplatform.core import tasks as tasks_module
from aiplatform.core.http import HttpError, Request, bad_request, found, not_found
from aiplatform.core.narrator import INSTALL_HINT, NarratorError, http_error

PREFIX = "library"
URL_BASE = f"/api/{PREFIX}"
GPU_OWNER = "Статьи"
MAX_BODY = 2 * 1024 * 1024
_STAGE = re.compile(r"абзац (\d+)/(\d+)")
_ARTICLE = re.compile(r"^=== (\d+)/(\d+) ")
_ID = re.compile(r"^[\w.-]{1,200}$")


def progress(line: str) -> str | None:
    """Строка вывода движка → этап задачи: абзац озвучки, статья пачки, распознавание."""
    if m := _ARTICLE.match(line):
        return f"Статья {m[1]} из {m[2]}"
    if m := _STAGE.search(line):
        return f"Абзац {m[1]} из {m[2]}"
    if "распозна" in line:
        return "Проверка распознаванием"
    if line.startswith("телеграм"):
        return "Отправка в Telegram"
    return None


class LibraryModule:
    prefix = PREFIX
    label = "Статьи"

    def __init__(self, ctx) -> None:
        self.platform: Config = ctx.config
        self.narrator = narrator_module.load(ctx.config)
        self.tasks = tasks_module.Tasks(ctx.config.data_dir / "library" / "tasks", ctx.gate)

    def describe(self) -> dict:
        return {}

    def problems(self) -> list[str]:
        problem = self.narrator.check()  # кеш общий: одинаковый текст App.problems склеит
        return [problem] if problem else []

    # --- маршруты ---

    def handle(self, req: Request) -> None:
        match (req.method, req.sub):
            case ("GET", ["state"]):
                return req.send_json(self.state())
            case ("POST", ["articles"]):
                return req.send_json(self.add(req.json(MAX_BODY)), HTTPStatus.CREATED)
            case ("GET", ["articles", article_id]):
                return req.send_json(self.article(_checked(article_id)))
            case ("POST", ["articles", article_id]):
                return req.send_json(self.save(_checked(article_id), req.json(MAX_BODY)))
            case ("GET", ["articles", article_id, "audio", engine]):
                path = self.audio(_checked(article_id), engine)
                attachment = path.name if "download" in req.query else None
                return req.send_file(path, "audio/mpeg", ranged=True, attachment=attachment)
            case ("POST", ["articles", article_id, action]):
                return req.send_json(self.act(_checked(article_id), action, req.json()), HTTPStatus.ACCEPTED)
            case ("POST", ["render"]):
                return req.send_json(self.render(req.json()), HTTPStatus.ACCEPTED)
            case ("POST", ["unsent", mode]) if mode in ("send", "mark"):
                return req.send_json(self.unsent(mode), HTTPStatus.ACCEPTED)
            case ("GET", ["tasks", task_id]):
                return req.send_json(found(self.tasks.get(task_id)))
            case ("POST", ["tasks", task_id, "cancel"]):
                return req.send_json(found(self.tasks.cancel(task_id)))
        raise not_found()

    # --- чтение ---

    def _bridge(self, *args: str, stdin: str | dict | None = None, timeout: float = 120) -> dict:
        try:
            return self.narrator.bridge(*args, stdin=stdin, timeout=timeout)
        except NarratorError as exc:
            raise http_error(exc) from exc

    def state(self) -> dict:
        data = self._bridge("library")
        try:
            channels = self.narrator.bridge("bot").get("channels") or {}
        except NarratorError:
            channels = {}
        return {
            "articles": data["articles"],
            "unsent": data["unsent"],
            "root": data["root"],
            "channels": sorted(channels),
            "tasks": self.tasks.list()[:12],
        }

    def article(self, article_id: str) -> dict:
        card = self._bridge("article", article_id)
        card["audio_urls"] = {
            engine: f"{URL_BASE}/articles/{quote(article_id)}/audio/{engine}" for engine in card.get("audio") or {}
        }
        card.pop("audio", None)
        card.pop("folder", None)
        card["tasks"] = self.tasks.list(target=article_id)[:6]
        return card

    def audio(self, article_id: str, engine: str) -> Path:
        path = (self._bridge("article", article_id).get("audio") or {}).get(engine)
        if not path or not Path(path).is_file():
            raise not_found("Озвучки этим движком нет.")
        return Path(path)

    # --- запись ---

    def add(self, payload: object) -> dict:
        """По ссылке — движок скачивает статью; иначе — присланный текст как есть."""
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        url = str(payload.get("url") or "").strip()
        if url and not payload.get("text"):
            if not re.match(r"^https?://\S+$", url):
                raise bad_request("Ссылка — вида https://…")
            card = self._bridge("fetch", url, timeout=180)
        else:
            card = self._bridge("article-new", stdin={k: payload.get(k) for k in ("title", "text", "url", "summary")})
        return self.article(card["id"])

    def save(self, article_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        changes = {k: payload[k] for k in ("title", "summary", "script") if isinstance(payload.get(k), str)}
        if not changes:
            raise bad_request("Нечего сохранять: title, summary или script.")
        self._bridge("article-save", article_id, stdin=changes)
        return self.article(article_id)

    def _start(self, kind: str, title: str, argv: list[str], *, target: str = "", gpu: bool = False) -> dict:
        busy = self.tasks.busy(kind, target or None)
        if busy and target:
            raise HttpError(HTTPStatus.CONFLICT, f"Уже идёт: {busy['title']}.")
        spec = tasks_module.Spec(
            kind=kind,
            title=title,
            argv=argv,
            cwd=self.narrator.cwd,
            env=self.narrator.env,
            gpu=GPU_OWNER if gpu else None,
            target=target,
            hint=INSTALL_HINT,
            progress=progress,
        )
        return self.tasks.start(spec)

    def act(self, article_id: str, action: str, payload: object) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        card = self._bridge("article", article_id)
        name = card["title"]
        match action:
            case "synth":
                engines = voices.enabled(self.platform.settings)
                argv = self.narrator.argv("synth", article_id, *engine_args(payload, engines))
                if not payload.get("send"):
                    argv.append("--no-send")
                return self._start("synth", f"Озвучка: {name}", argv, target=article_id, gpu=True)
            case "verify":
                if not card.get("has_audio"):
                    raise bad_request("Сначала озвучь статью.")
                argv = self.narrator.argv("verify", article_id)
                return self._start("verify", f"Проверка: {name}", argv, target=article_id, gpu=True)
            case "send":
                if not card.get("has_audio"):
                    raise bad_request("Сначала озвучь статью.")
                argv = self.narrator.argv("bot", "send", article_id, "--force")
                channel = str(payload.get("channel") or "").strip()
                if channel:
                    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*|-?\d+(?:_\d+)?|@\w+", channel):
                        raise bad_request("Канал — имя из telegram_tems.md или id.")
                    argv += ["--channel", channel]
                where = f" в {channel}" if channel else ""
                return self._start("send", f"Отправка{where}: {name}", argv, target=article_id)
            case "prepare":
                if not card.get("has_source"):
                    raise bad_request("У статьи нет исходного текста — агенту не из чего готовить.")
                argv = self.narrator.make("article-prepare", A=article_id)
                return self._start("prepare", f"Агент готовит текст: {name}", argv, target=article_id)
        raise not_found()

    def render(self, payload: object) -> dict:
        payload = payload if isinstance(payload, dict) else {}
        everything = bool(payload.get("all"))
        argv = self.narrator.argv("render", *engine_args(payload, voices.enabled(self.platform.settings)))
        if everything:
            argv.append("--all")
        if not payload.get("send", True):
            argv.append("--no-send")
        title = "Переозвучить все статьи" if everything else "Озвучить новые статьи"
        if self.tasks.busy("render"):
            raise HttpError(HTTPStatus.CONFLICT, "Озвучка статей уже идёт.")
        return self._start("render", title, argv, gpu=True)

    def unsent(self, mode: str) -> dict:
        if self.tasks.busy("unsent"):
            raise HttpError(HTTPStatus.CONFLICT, "Рассылка уже идёт.")
        if mode == "mark":
            argv = self.narrator.argv("bot", "unsent", "--mark")
            return self._start("unsent", "Считать всё отправленным", argv)
        return self._start("unsent", "Отправить неотправленные", self.narrator.argv("bot", "unsent"))

    def close(self) -> None:
        self.tasks.close()


def engine_args(payload: dict, engines: dict[str, voices.Engine] | None = None) -> list[str]:
    """--engine/--voice/--speed/--steps из запроса, проверенные по включённым движкам."""
    engines = voices.ENGINES if engines is None else engines
    engine = engines.get(str(payload.get("engine") or "")) if payload.get("engine") else None
    if payload.get("engine") and engine is None:
        raise bad_request(voices.unknown(payload.get("engine")))
    if engine is None:
        return []
    out = ["--engine", engine.id]
    voice = payload.get("voice")
    if voice:
        if voice not in engine.voices:
            raise bad_request(voices.unknown(engine.id, voice))
        out += ["--voice", voice]
    speed = payload.get("speed")
    if engine.tunable and isinstance(speed, int | float) and not isinstance(speed, bool):
        low, high = voices.SPEED_RANGE
        if not low <= speed <= high:
            raise bad_request(f"Скорость — от {low:g} до {high:g}.")
        out += ["--speed", f"{speed:g}"]
    steps = payload.get("steps")
    if engine.tunable and isinstance(steps, int) and not isinstance(steps, bool):
        if not 1 <= steps <= 128:
            raise bad_request("Шаги — от 1 до 128.")
        out += ["--steps", str(steps)]
    return out


def _checked(article_id: str) -> str:
    if not _ID.match(article_id or ""):
        raise not_found("Нет такой статьи.")
    return article_id
