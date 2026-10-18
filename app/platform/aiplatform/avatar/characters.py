"""Персонажи диктора: картинка и имя в `data/avatar/characters/<id>/`.

Картинка при создании переезжает из uploads в каталог персонажа: уборка
загрузок её не тронет, а персонаж живёт, пока его не удалят. У фото
человека — согласие на использование лица (как у голоса, CMP-010).
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path

from aiplatform.core import cleanup
from aiplatform.core.fsutil import atomic_write
from aiplatform.core.uploads import IMAGE_MIME

KINDS = {"photo": "Фото человека", "drawn": "Рисованный"}
CONSENT_FORMS = {"self": "Это моё лицо", "written": "Есть письменное согласие"}
MAX_NAME = 80
MAX_OWNER = 200
FILE = "character.json"
_ID = re.compile(r"^[0-9a-f]{12}$")


class CharacterError(ValueError):
    pass


def _name(raw: object) -> str:
    name = " ".join(raw.split()) if isinstance(raw, str) else ""
    if not name:
        raise CharacterError("Дай персонажу имя.")
    if len(name) > MAX_NAME:
        raise CharacterError(f"Имя персонажа — до {MAX_NAME} знаков.")
    return name


def consent_of(raw: object) -> dict:
    """{"confirmed": true, "owner": "чьё лицо", "form": "self"|"written"} — иначе CharacterError."""
    if not isinstance(raw, dict) or raw.get("confirmed") is not True:
        raise CharacterError("Подтверди, что это твоё лицо или у тебя есть письменное согласие человека на фото.")
    owner = " ".join(raw["owner"].split()) if isinstance(raw.get("owner"), str) else ""
    if not owner or len(owner) > MAX_OWNER:
        raise CharacterError("Напиши, чьё лицо на фото.")
    form = raw.get("form", "self")
    if form not in CONSENT_FORMS:
        raise CharacterError("Согласие — self (моё лицо) или written (письменное согласие).")
    return {"confirmed": True, "owner": owner, "form": form}


class CharacterStore:
    def __init__(self, root: Path, url_base: str) -> None:
        self.root = root
        self.url = url_base
        self._lock = threading.Lock()
        root.mkdir(parents=True, exist_ok=True)

    def _dir(self, char_id: str) -> Path | None:
        if not isinstance(char_id, str) or not _ID.match(char_id):
            return None
        return self.root / char_id

    def _read(self, folder: Path) -> dict | None:
        try:
            data = json.loads((folder / FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) and data.get("id") == folder.name else None

    def _view(self, item: dict) -> dict:
        view = {k: v for k, v in item.items() if k != "consent"}
        view["image_url"] = f"{self.url}/characters/{item['id']}/image"
        view["kind_label"] = KINDS.get(item.get("kind"), "")
        return view

    def list(self) -> list[dict]:
        found = [self._read(p) for p in self.root.iterdir() if p.is_dir()] if self.root.is_dir() else []
        return [self._view(c) for c in sorted(filter(None, found), key=lambda c: c["created_at"])]

    def get(self, char_id: str) -> dict | None:
        folder = self._dir(char_id)
        item = self._read(folder) if folder else None
        return self._view(item) if item else None

    def image(self, char_id: str) -> Path | None:
        folder = self._dir(char_id)
        item = self._read(folder) if folder else None
        if item is None:
            return None
        path = folder / f"image.{item['ext']}"
        return path if path.is_file() else None

    def create(self, payload: object, source: Path | None) -> dict:
        """payload: name, kind, consent (для фото). source — загруженная картинка (переедет сюда)."""
        if not isinstance(payload, dict):
            raise CharacterError("Тело запроса — не объект JSON.")
        name = _name(payload.get("name"))
        kind = payload.get("kind", "photo")
        if kind not in KINDS:
            raise CharacterError("Вид персонажа — photo (фото человека) или drawn (рисованный).")
        consent = consent_of(payload.get("consent")) if kind == "photo" else None
        if source is None:
            raise CharacterError("Загрузи картинку персонажа ещё раз — файл не найден.")
        ext = source.suffix.lstrip(".")
        if ext not in IMAGE_MIME:
            raise CharacterError("Картинка персонажа — PNG, JPEG или WebP.")
        char_id = uuid.uuid4().hex[:12]
        folder = self.root / char_id
        folder.mkdir(parents=True)
        shutil.move(str(source), folder / f"image.{ext}")
        item = {"id": char_id, "name": name, "kind": kind, "ext": ext, "created_at": time.time()}
        if consent:
            item["consent"] = consent | {"at": item["created_at"]}
        self._save(folder, item)
        return self._view(item)

    def rename(self, char_id: str, payload: object) -> dict | None:
        name = _name(payload.get("name") if isinstance(payload, dict) else None)
        with self._lock:
            folder = self._dir(char_id)
            item = self._read(folder) if folder else None
            if item is None:
                return None
            item["name"] = name
            self._save(folder, item)
        return self._view(item)

    def delete(self, char_id: str) -> bool:
        folder = self._dir(char_id)
        if folder is None or self._read(folder) is None:
            return False
        cleanup.remove_tree(folder)
        return True

    @staticmethod
    def _save(folder: Path, item: dict) -> None:
        atomic_write(folder / FILE, json.dumps(item, ensure_ascii=False, indent=2))
