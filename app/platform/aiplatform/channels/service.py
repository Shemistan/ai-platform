"""Вкладка «Каналы»: куда уходит контент и кто его получает, API под /api/channels/.

- Каналы публикации — тот же channels.json, что в шаге «Публикация» сценариев:
  список, добавить, поправить, убрать, взять каналы движка (telegram_tems.md).
- Бот движка: найден ли токен, как бот называется.
- Подписчики бота и приём подписок: `narrate bot serve` в фоне, пока не
  остановят. Подписчикам озвучки статей идут в личку, когда канал статей
  выключен (NARRATOR_TELEGRAM_CHANNEL=off); иначе — в канал.
"""

from __future__ import annotations

import threading
import time
from http import HTTPStatus

from aiplatform.core import narrator as narrator_module
from aiplatform.core import runner
from aiplatform.core.http import HttpError, Request, bad_request, not_found
from aiplatform.core.narrator import INSTALL_HINT, NarratorError
from aiplatform.studio import config as studio_config
from aiplatform.studio import publish

PREFIX = "channels"
LOG_TAIL = 200


class BotServe:
    """`narrate bot serve` в фоне: принимает /start и /stop, пока не остановят."""

    def __init__(self, narrator) -> None:
        self._narrator = narrator
        self._lock = threading.Lock()
        self._cancel: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._state = {"running": False, "started_at": None, "stopped_at": None, "error": None, "log": []}

    def view(self) -> dict:
        with self._lock:
            return {**self._state, "log": list(self._state["log"][-40:])}

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
            if line.strip():
                with self._lock:
                    self._state["log"].append(line.rstrip())
                    del self._state["log"][:-LOG_TAIL]

        error = None
        try:
            argv = self._narrator.argv("bot", "serve")
            runner.run(argv, on_line, cancel, cwd=self._narrator.cwd, env=self._narrator.env, missing_hint=INSTALL_HINT)
            error = "Бот остановился сам — причина в логе."
        except runner.Canceled:
            pass
        except runner.RunFailed as exc:
            error = str(exc)
        with self._lock:
            self._state.update(running=False, stopped_at=time.time(), error=error)

    def close(self) -> None:
        self.stop()


class ChannelsModule:
    prefix = PREFIX
    label = "Каналы"

    def __init__(self, ctx) -> None:
        self.config = studio_config.load(ctx.config)
        self.narrator = narrator_module.load(ctx.config)
        self.serve = BotServe(self.narrator)

    def describe(self) -> dict:
        return {"channels_file": str(self.config.channels_file)}

    def problems(self) -> list[str]:
        return []

    # --- данные ---

    def _bridge(self, *args: str) -> dict:
        try:
            return self.narrator.bridge(*args)
        except NarratorError as exc:
            raise HttpError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc

    def _channels(self) -> list[publish.Channel]:
        try:
            return self.config.channels()
        except ValueError as exc:
            raise bad_request(f"Каналы: {exc}") from exc

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
        match (req.method, req.sub):
            case ("GET", ["state"]):
                return req.send_json(self.state())
            case ("POST", ["channels"]):
                return req.send_json(self.add(req.json()), HTTPStatus.CREATED)
            case ("POST", ["channels", "import"]):
                return req.send_json(self.import_narrator())
            case ("POST", ["channels", channel_id, "delete"]):
                return req.send_json(self.delete(channel_id))
            case ("POST", ["channels", channel_id]):
                return req.send_json(self.edit(channel_id, req.json()))
            case ("POST", ["bot", "check"]):
                return req.send_json(self._bridge("bot", "--check"))
            case ("GET", ["subscribers"]):
                sync = req.arg("sync") == "1"
                return req.send_json(self._bridge("subscribers", *(["--sync"] if sync else [])))
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
        current = self._channels()
        try:
            channel = publish.new_channel(
                current,
                str(payload.get("label") or ""),
                str(payload.get("chat_id") or ""),
                str(payload.get("format") or publish.LANDSCAPE),
            )
            if payload.get("token_env"):
                [channel] = publish.edit_channel([channel], channel.id, {"token_env": payload["token_env"]})
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        publish.save_channels(self.config.channels_file, [*current, channel])
        return self.channels_view() | {"added": channel.id}

    def edit(self, channel_id: str, payload: object) -> dict:
        if not isinstance(payload, dict):
            raise bad_request("Тело запроса — не объект JSON.")
        try:
            changed = publish.edit_channel(self._channels(), channel_id, payload)
        except LookupError as exc:
            raise not_found(str(exc)) from exc
        except ValueError as exc:
            raise bad_request(str(exc)) from exc
        publish.save_channels(self.config.channels_file, changed)
        return self.channels_view()

    def delete(self, channel_id: str) -> dict:
        current = self._channels()
        if channel_id not in {c.id for c in current}:
            raise not_found("Нет такого канала.")
        publish.save_channels(self.config.channels_file, [c for c in current if c.id != channel_id])
        return self.channels_view()

    def import_narrator(self) -> dict:
        """Каналы движка, которых ещё нет в списке, — в channels.json."""
        current = self._channels()
        names = self._bridge("bot").get("channels") or {}
        known = {c.chat_id for c in current}
        taken = {c.id for c in current}
        added = []
        for raw in publish.from_narrator(names):
            if raw["chat_id"] in known or raw["id"] in taken:
                continue
            added.append(publish.Channel(raw["id"], raw["label"], publish.TELEGRAM, chat_id=raw["chat_id"]))
        publish.save_channels(self.config.channels_file, [*current, *added])
        return self.channels_view() | {"imported": [c.id for c in added]}

    def close(self) -> None:
        self.serve.close()
