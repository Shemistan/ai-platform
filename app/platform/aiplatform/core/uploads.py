"""Хранилище загрузок: картинки и аудио, тип определяется по сигнатуре."""

from __future__ import annotations

import os
import re
import struct
import uuid
from pathlib import Path

from aiplatform.core.fsutil import atomic_write

MAX_UPLOAD_BYTES = 25 * 1024 * 1024
MAX_AUDIO_BYTES = 200 * 1024 * 1024
IMAGE_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
AUDIO_MIME = {"mp3": "audio/mpeg", "wav": "audio/wav", "m4a": "audio/mp4"}
MIME = IMAGE_MIME | AUDIO_MIME
IMAGE = "image"
AUDIO = "audio"
_ID = re.compile(r"^[0-9a-f]{32}$")
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class UploadError(ValueError):
    """Файл не принят; текст показывается пользователю."""


def sniff(data: bytes) -> str | None:
    if data.startswith(_PNG_SIGNATURE):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data.startswith(b"ID3") or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    if data[4:8] == b"ftyp":
        return "m4a"
    return None


VIDEO_EXT = ("mp4", "mov", "webm")


def sniff_video(data: bytes) -> str | None:
    """Видеоролик по сигнатуре: MP4/MOV (контейнер ISO, кроме аудио M4A) или WebM."""
    if data[4:8] == b"ftyp":
        brand = data[8:12]
        if brand in (b"M4A ", b"M4B ", b"M4P "):
            return None
        return "mov" if brand == b"qt  " else "mp4"
    if data.startswith(b"\x1a\x45\xdf\xa3"):
        return "webm"
    return None


def head(path: Path, n: int = 64) -> bytes:
    """Первые байты файла — для сигнатуры, не читая файл целиком."""
    with path.open("rb") as fh:
        return fh.read(n)


def kind_of(ext: str) -> str:
    return IMAGE if ext in IMAGE_MIME else AUDIO


def png_size(data: bytes) -> tuple[int, int] | None:
    """Ширина и высота из заголовка IHDR; None — не PNG или заголовок битый."""
    if not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR" or len(data) < 24:
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None


class UploadStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def save(self, data: bytes, kinds: tuple[str, ...] = (IMAGE,)) -> str:
        if not data:
            raise UploadError("Пустой файл.")
        ext = sniff(data)
        if ext is None or kind_of(ext) not in kinds:
            raise UploadError(_accepted(kinds))
        limit = MAX_UPLOAD_BYTES if kind_of(ext) == IMAGE else MAX_AUDIO_BYTES
        if len(data) > limit:
            raise UploadError(f"Файл больше {limit // 2**20} МБ.")
        upload_id = uuid.uuid4().hex
        atomic_write(self.root / f"{upload_id}.{ext}", data)
        return upload_id

    def save_file(self, tmp: Path, kinds: tuple[str, ...] = (IMAGE,)) -> str:
        """Загрузка, уже записанная на диск (body_to_file), — в хранилище переименованием.

        Тип — по первым байтам, размер — по файлу; не принят — временный файл удаляется.
        """
        try:
            size = tmp.stat().st_size
            if not size:
                raise UploadError("Пустой файл.")
            ext = sniff(head(tmp))
            if ext is None or kind_of(ext) not in kinds:
                raise UploadError(_accepted(kinds))
            limit = MAX_UPLOAD_BYTES if kind_of(ext) == IMAGE else MAX_AUDIO_BYTES
            if size > limit:
                raise UploadError(f"Файл больше {limit // 2**20} МБ.")
            upload_id = uuid.uuid4().hex
            os.replace(tmp, self.root / f"{upload_id}.{ext}")
            return upload_id
        finally:
            tmp.unlink(missing_ok=True)

    def path(self, upload_id: str) -> Path | None:
        if not isinstance(upload_id, str) or not _ID.match(upload_id):
            return None
        for ext in MIME:
            candidate = self.root / f"{upload_id}.{ext}"
            if candidate.is_file():
                return candidate
        return None

    def size(self, upload_id: str) -> tuple[int, int] | None:
        """Размер картинки в пикселях; известен только для PNG."""
        path = self.path(upload_id)
        if path is None or path.suffix != ".png":
            return None
        with path.open("rb") as fh:
            return png_size(fh.read(24))


def _accepted(kinds: tuple[str, ...]) -> str:
    names = []
    if IMAGE in kinds:
        names.append("PNG, JPEG, WebP")
    if AUDIO in kinds:
        names.append("MP3, WAV, M4A")
    return "Поддерживаются только " + "; ".join(names) + "."
