"""Единственный владелец channels.json: чтение и все правки под одним замком.

И вкладка «Каналы», и сценарии читают и меняют список каналов только здесь:
load → изменить → атомарно записать идут под замком файла, поэтому две
вкладки или двойной клик не теряют канал. Битый файл — ошибка конфигурации
сервера (ChannelsBroken), а не запроса.
"""

from __future__ import annotations

import threading
from pathlib import Path

from aiplatform.studio import publish

_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    """Замок на файл, общий для всех ChannelStore процесса: модули создают свои экземпляры."""
    key = Path(path).expanduser().absolute()
    with _GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


class ChannelsBroken(ValueError):
    """channels.json (или telegram_tems.md narrator) не читается — чинить надо файл на сервере."""


class ChannelStore:
    def __init__(self, path: Path, narrator_dir: Path | None = None) -> None:
        self.path = Path(path)
        self.narrator_dir = narrator_dir
        self.lock = _lock_for(self.path)

    def load(self) -> list[publish.Channel]:
        try:
            return publish.load_channels(self.path, self.narrator_dir)
        except ValueError as exc:
            raise ChannelsBroken(str(exc)) from exc

    def _save(self, channels: list[publish.Channel]) -> None:
        publish.save_channels(self.path, channels)

    def add(self, payload: dict) -> publish.Channel:
        """Канал Telegram со страницы (label, chat_id, format, token_env); ValueError — неверный ввод."""
        with self.lock:
            current = self.load()
            channel = publish.new_channel(
                current,
                str(payload.get("label") or ""),
                str(payload.get("chat_id") or ""),
                str(payload.get("format") or publish.LANDSCAPE),
            )
            if payload.get("token_env"):
                [channel] = publish.edit_channel([channel], channel.id, {"token_env": payload["token_env"]})
            self._save([*current, channel])
            return channel

    def edit(self, channel_id: str, changes: dict) -> None:
        """LookupError — нет канала, ValueError — неверная правка."""
        with self.lock:
            self._save(publish.edit_channel(self.load(), channel_id, changes))

    def delete(self, channel_id: str) -> None:
        with self.lock:
            current = self.load()
            if channel_id not in {c.id for c in current}:
                raise LookupError("Нет такого канала.")
            self._save([c for c in current if c.id != channel_id])

    def import_narrator(self, names: dict[str, str]) -> list[str]:
        """Каналы движка (имя → id), которых ещё нет в списке; возвращает id добавленных."""
        with self.lock:
            current = self.load()
            known = {c.chat_id for c in current}
            taken = {c.id for c in current}
            added = []
            for raw in publish.from_narrator(names):
                if raw["chat_id"] in known or raw["id"] in taken:
                    continue
                added.append(publish.Channel(raw["id"], raw["label"], publish.TELEGRAM, chat_id=raw["chat_id"]))
            self._save([*current, *added])
            return [c.id for c in added]
