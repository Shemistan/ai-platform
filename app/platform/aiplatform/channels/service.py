"""Вкладка «Каналы»: куда уходит контент и кто его получает, API под /api/channels/.

- Каналы публикации — тот же channels.json, что в шаге «Публикация» сценариев:
  список, добавить, поправить, убрать, взять каналы движка (telegram_tems.md).
- Бот движка: найден ли токен, как бот называется.
- Подписчики бота и приём подписок: `narrate bot serve` в фоне, пока не
  остановят. Подписчикам озвучки статей идут в личку, когда канал статей
  выключен (NARRATOR_TELEGRAM_CHANNEL=off); иначе — в канал.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from http import HTTPStatus
from pathlib import Path

from aiplatform.channels.store import ChannelsBroken
from aiplatform.config import pick
from aiplatform.core import log, perms, runner
from aiplatform.core import narrator as narrator_module
from aiplatform.core.http import HttpError, Request, bad_request, not_found
from aiplatform.core.narrator import INSTALL_HINT, NarratorError, http_error
from aiplatform.studio import config as studio_config
from aiplatform.studio import publish

PREFIX = "channels"
TOKEN_FILE_ENV = "NARRATOR_TELEGRAM_TOKEN_FILE"  # как narrator.telegram.TOKEN_FILE_ENV
TOKEN_FILE = "bot_token.md"  # как narrator.telegram.TOKEN_FILE
LIBRARY_ENV = "NARRATOR_LIBRARY"  # как narrator.library.library_root
SERVE_LOCK = "telegram.serve.lock"  # как narrator.telegram.SERVE_LOCK
LOG_TAIL = 200
UPDATE = "update "  # строка `bot serve --updates`: нажатие кнопки или ответ боту, дальше — JSON


@contextlib.contextmanager
def channel_errors() -> Iterator[None]:
    """Ошибки ChannelStore → HTTP: битый файл — 503 (чинить сервер), нет канала — 404, ввод — 400."""
    try:
        yield
    except ChannelsBroken as exc:
        raise HttpError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            f"Каналы: файл настроек сломан, правки недоступны — {exc}",
            code="config_broken",
        ) from exc
    except LookupError as exc:
        raise not_found(str(exc)) from exc
    except ValueError as exc:
        raise bad_request(str(exc)) from exc


def lock_held(path: Path | None) -> bool:
    """Держит ли кто-то flock на замке `bot serve` (как narrator.telegram.serve_running)."""
    if path is None or not path.is_file():
        return False
    try:
        with open(path) as fh:
            try:
                fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(fh, fcntl.LOCK_UN)
    except OSError:
        return False
    return False


class BotServe:
    """`narrate bot serve` в фоне: принимает /start и /stop, пока не остановят.

    Нажатия кнопок и ответы боту serve печатает строкой `update {json}` — их получает
    on_update (ревью выпуска в личке владельца, PRD-001), в лог вкладки они не идут."""

    def __init__(self, narrator, lock: Path | None = None) -> None:
        self._narrator = narrator
        self.lock = lock  # SERVE_LOCK движка: его держит любой `bot serve`, наш или из терминала
        self.on_update: Callable[[dict], None] | None = None
        self._lock = threading.Lock()
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._state = {"running": False, "started_at": None, "stopped_at": None, "error": None, "log": []}

    def view(self) -> dict:
        with self._lock:
            view = {**self._state, "log": list(self._state["log"][-40:])}
        # Не наш serve держит замок — бот слушает чужой процесс: нажатия в ревью сюда не дойдут (PRD-016).
        view["external"] = not view["running"] and lock_held(self.lock)
        return view

    def start(self) -> dict:
        with self._lock:
            if self._state["running"]:
                return {**self._state}
            self._cancel = threading.Event()
            self._state.update(running=True, started_at=time.time(), stopped_at=None, error=None, log=[])
            self._thread = threading.Thread(target=self._run, args=(self._cancel,), name="bot-serve", daemon=True)
            self._thread.start()
        return self.view()

    def stop(self) -> dict:
        with self._lock:
            cancel, thread = self._cancel, self._thread
        if cancel:
            cancel.set()
        if thread:
            thread.join(timeout=10)
        return self.view()

    def _run(self, cancel: threading.Event) -> None:
        def on_line(line: str) -> None:
            if line.startswith(UPDATE):
                self._dispatch(line[len(UPDATE) :])
            elif line.strip():
                with self._lock:
                    self._state["log"].append(line.rstrip())
                    del self._state["log"][:-LOG_TAIL]

        error = None
        try:
            argv = self._narrator.argv("bot", "serve", "--updates")
            runner.run(argv, on_line, cancel, cwd=self._narrator.cwd, env=self._narrator.env, missing_hint=INSTALL_HINT)
            error = "Бот остановился сам — причина в логе."
        except runner.Canceled:
            pass
        except runner.RunFailed as exc:
            error = str(exc)
        with self._lock:
            self._state.update(running=False, stopped_at=time.time(), error=error)

    def _dispatch(self, raw: str) -> None:
        handler = self.on_update
        if handler is None:
            return
        try:
            handler(json.loads(raw))
        except Exception as exc:  # noqa: BLE001 — сбой одного нажатия не роняет приём подписок
            log.event("bot.update_failed", level=logging.WARNING, error=f"{type(exc).__name__}: {exc}")

    def close(self) -> None:
        self.stop()


class ChannelsModule:
    prefix = PREFIX
    label = "Каналы"

    def __init__(self, ctx) -> None:
        self.config = studio_config.load(ctx.config)
        self.narrator = narrator_module.load(ctx.config)
        self.env = ctx.config.env
        self.serve = BotServe(self.narrator, None if self.narrator.fake else self.serve_lock())
        self.store = self.config.channel_store()

    def serve_lock(self) -> Path:
        """Замок `bot serve` в библиотеке движка (narrator.telegram.SERVE_LOCK)."""
        library = pick(self.env, LIBRARY_ENV) or str(self.narrator.home / "library")
        return Path(library).expanduser() / SERVE_LOCK

    def describe(self) -> dict:
        return {"channels_file": str(self.config.channels_file)}

    def problems(self) -> list[str]:
        out = []
        problem = self.narrator.check()
        if problem:
            out.append(problem)
        try:
            self.store.load()
        except ChannelsBroken as exc:  # каналы — забота этой вкладки: баннер один, даже если сценарии тоже читают
            out.append(f"Каналы: {exc}")
        out.extend(self.secret_problems())
        return out

    def secret_files(self) -> list[tuple[Path, str]]:
        """Файлы движка с токеном, каналами и подписчиками — им нужны права 0600 (SEC-010)."""
        home = self.narrator.home
        token = pick(self.env, TOKEN_FILE_ENV) or str(home / TOKEN_FILE)
        return [
            (Path(token).expanduser(), "Файл токена бота"),
            (home / "telegram_tems.md", "Список каналов"),
            (home / "library" / "telegram.json", "Файл подписчиков бота"),
            (self.config.channels_file, "Файл каналов публикации"),
        ]

    def secret_problems(self) -> list[str]:
        """Предупреждения о правах шире 0600; в демо файлы фейковые — молчим."""
        if self.narrator.fake:
            return []
        return [w for path, what in self.secret_files() if (w := perms.warning(path, what))]

    # --- данные ---

    def _bridge(self, *args: str) -> dict:
        try:
            return self.narrator.bridge(*args)
        except NarratorError as exc:
            raise http_error(exc) from exc

    def channels_view(self) -> dict:
        try:
            channels = [c.public() for c in self.config.channels()]
            error = None
        except ValueError as exc:
            channels, error = [], str(exc)
        return {
            "channels": channels,
            "error": error,
            "file": str(self.config.channels_file),
            "custom": self.config.channels_file.is_file(),
        }

    def state(self) -> dict:
        try:
            bot = self.narrator.bridge("bot")
        except NarratorError as exc:
            bot = {"token": False, "token_error": str(exc), "channels": {}}
        try:
            unsent = len(self.narrator.bridge("unsent")["unsent"])
        except NarratorError:
            unsent = None
        known = {c["chat_id"] for c in self.channels_view()["channels"]}
        importable = [c for c in publish.from_narrator(bot.get("channels") or {}) if c["chat_id"] not in known]
        return self.channels_view() | {
            "bot": bot,
            "importable": importable,
            "serve": self.serve.view(),
            "unsent": unsent,
        }

    # --- маршруты ---

    def handle(self, req: Request) -> None:
        # Каналы — ресурс items: /api/channels/items[/<id>]. Старый /api/channels/channels… — алиас (BE-011).
        sub = ["items", *req.sub[1:]] if req.sub[:1] == ["channels"] else req.sub
        match (req.method, sub):
            case ("GET", ["state"]):
                return req.send_json(self.state())
            case ("POST", ["items"]):
                view = self.add(req.json())
                location = f"/api/channels/items/{view['added']}"
                return req.send_json(view, HTTPStatus.CREATED, {"Location": location})
            case ("POST", ["items", "import"]):
                return req.send_json(self.import_narrator())
            case ("POST", ["items", channel_id, "delete"]):
                return req.send_json(self.delete(channel_id))
            case ("POST", ["items", channel_id]):
                return req.send_json(self.edit(channel_id, req.json()))
            case ("POST", ["bot", "check"]):
                return req.send_json(self._bridge("bot", "--check"))
            case ("GET", ["subscribers"]):
                # GET только читает: getUpdates сдвигает offset и пишет telegram.json,
                # а GET не проверяется по Origin (SEC-008).
                return req.send_json(self._bridge("subscribers"))
            case ("POST", ["subscribers", "sync"]):
                return req.send_json(self._bridge("subscribers", "--sync"))
            case ("GET", ["serve"]):
                return req.send_json(self.serve.view())
            case ("POST", ["serve", "start"]):
                return req.send_json(self.serve.start())
            case ("POST", ["serve", "stop"]):
                return req.send_json(self.serve.stop())
        raise not_found()

    def add(self, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        with channel_errors():
            channel = self.store.add(payload)
        return self.channels_view() | {"added": channel.id}

    def edit(self, channel_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        with channel_errors():
            self.store.edit(channel_id, payload)
        return self.channels_view()

    def delete(self, channel_id: str) -> dict:
        with channel_errors():
            self.store.delete(channel_id)
        return self.channels_view()

    def import_narrator(self) -> dict:
        """Каналы движка, которых ещё нет в списке, — в channels.json."""
        with channel_errors():
            self.store.load()  # битый файл — 503 до похода в мост
        names = self._bridge("bot").get("channels") or {}
        with channel_errors():
            added = self.store.import_narrator(names)
        return self.channels_view() | {"imported": added}

    def close(self) -> None:
        self.serve.close()
