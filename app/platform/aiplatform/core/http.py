"""Запрос и ответ для модулей-вкладок: тело, JSON, файлы с Range, ошибки."""

from __future__ import annotations

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from aiplatform.core.uploads import IMAGE, MAX_AUDIO_BYTES, MAX_UPLOAD_BYTES, MIME, UploadError, UploadStore

MAX_JSON_BYTES = 1024 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class HttpError(Exception):
    """extra — дополнительные поля ответа рядом с error (например, existing у дубликата)."""

    def __init__(self, status: HTTPStatus, message: str, extra: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}


def not_found(message: str = "Нет такого адреса.") -> HttpError:
    return HttpError(HTTPStatus.NOT_FOUND, message)


def bad_request(message: str) -> HttpError:
    return HttpError(HTTPStatus.BAD_REQUEST, message)


def found(value: dict | None, message: str = "Задача не найдена.") -> dict:
    if value is None:
        raise not_found(message)
    return value


class Request:
    """Один запрос к API модуля. sub — части пути после /api/<prefix>/."""

    def __init__(
        self,
        handler: BaseHTTPRequestHandler,
        method: str,
        sub: list[str],
        query: dict[str, list[str]],
        *,
        head: bool = False,
    ) -> None:
        self._h = handler
        self.method = method
        self.sub = sub
        self.query = query
        self.head = head

    @property
    def headers(self):
        return self._h.headers

    def arg(self, name: str, default: str | None = None) -> str | None:
        values = self.query.get(name)
        return values[0] if values else default

    # --- ввод ---

    def body(self, limit: int, too_big: str = "Слишком большой запрос.") -> bytes:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as exc:
            self._h.close_connection = True
            raise HttpError(HTTPStatus.LENGTH_REQUIRED, "Нужен заголовок Content-Length.") from exc
        if length > limit:
            self._h.close_connection = True  # тело не читаем — соединение дальше не годится
            raise HttpError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, too_big)
        return self._h.rfile.read(length)

    def json(self, limit: int = MAX_JSON_BYTES) -> object:
        raw = self.body(limit)
        try:
            return json.loads(raw or b"null")
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise bad_request("Тело запроса — не JSON.") from exc

    # --- вывод ---

    def send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h = self._h
        h.send_response(status)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        h.end_headers()
        if not self.head:
            h.wfile.write(data)

    def redirect(self, location: str) -> None:
        h = self._h
        h.send_response(HTTPStatus.FOUND)
        h.send_header("Location", location)
        h.send_header("Content-Length", "0")
        h.end_headers()

    def send_file(
        self,
        path: Path,
        ctype: str,
        *,
        cache: str = "no-store",
        attachment: str | None = None,
        ranged: bool = False,
    ) -> None:
        h = self._h
        size = path.stat().st_size
        start, end, status = 0, size - 1, HTTPStatus.OK
        header = self.headers.get("Range")
        if ranged and header:
            parsed = parse_range(header, size)
            if parsed is None:
                h.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                h.send_header("Content-Range", f"bytes */{size}")
                h.send_header("Content-Length", "0")
                h.end_headers()
                return
            start, end = parsed
            status = HTTPStatus.PARTIAL_CONTENT
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(max(end - start + 1, 0)))
        h.send_header("Cache-Control", cache)
        if attachment:
            h.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        if ranged:
            h.send_header("Accept-Ranges", "bytes")
        if status == HTTPStatus.PARTIAL_CONTENT:
            h.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        h.end_headers()
        if self.head:
            return
        with path.open("rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(remaining, 256 * 1024))
                if not chunk:
                    break
                h.wfile.write(chunk)
                remaining -= len(chunk)


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """`bytes=a-b` → (start, end) включительно; None — диапазон невыполним."""
    match = _RANGE.match(header.strip())
    if not match or size == 0:
        return None
    first, last = match.groups()
    if not first and not last:
        return None
    if not first:  # последние N байт
        start, end = max(size - int(last), 0), size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    return (start, end) if start <= end < size else None


def handle_uploads(req: Request, store: UploadStore, url_base: str, kinds: tuple[str, ...] = (IMAGE,)) -> bool:
    """`POST uploads` и `GET uploads/<id>` для модуля; False — путь не про загрузки."""
    match (req.method, req.sub):
        case ("POST", ["uploads"]):
            limit = MAX_AUDIO_BYTES if len(kinds) > 1 or IMAGE not in kinds else MAX_UPLOAD_BYTES
            data = req.body(limit, f"Файл больше {limit // 2**20} МБ.")
            try:
                upload_id = store.save(data, kinds)
            except UploadError as exc:
                raise bad_request(str(exc)) from exc
            width, height = store.size(upload_id) or (None, None)
            body = {"id": upload_id, "url": f"{url_base}/uploads/{upload_id}", "width": width, "height": height}
            req.send_json(body, HTTPStatus.CREATED)
            return True
        case ("GET", ["uploads", upload_id]):
            path = store.path(upload_id)
            if path is None:
                raise not_found("Файл не найден.")
            req.send_file(path, MIME[path.suffix[1:]], cache="private, max-age=86400", ranged=True)
            return True
    return False
