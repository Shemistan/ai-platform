"""Запрос и ответ для модулей-вкладок: тело, JSON, файлы с Range, ошибки."""

from __future__ import annotations

import json
import re
from email.utils import formatdate, parsedate_to_datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from aiplatform.core.uploads import IMAGE, MAX_AUDIO_BYTES, MAX_UPLOAD_BYTES, MIME, UploadError, UploadStore

MAX_JSON_BYTES = 1024 * 1024
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


# Машинный код ошибки по HTTP-статусу (BE-011, docs/api.md): клиент различает ошибки по code, не по тексту.
CODES = {
    HTTPStatus.BAD_REQUEST: "bad_request",
    HTTPStatus.UNAUTHORIZED: "unauthorized",
    HTTPStatus.FORBIDDEN: "forbidden",
    HTTPStatus.NOT_FOUND: "not_found",
    HTTPStatus.METHOD_NOT_ALLOWED: "method_not_allowed",
    HTTPStatus.CONFLICT: "conflict",
    HTTPStatus.LENGTH_REQUIRED: "length_required",
    HTTPStatus.REQUEST_ENTITY_TOO_LARGE: "too_large",
    HTTPStatus.MISDIRECTED_REQUEST: "bad_host",
    HTTPStatus.UNPROCESSABLE_ENTITY: "invalid",
    HTTPStatus.TOO_MANY_REQUESTS: "rate_limited",
    HTTPStatus.INTERNAL_SERVER_ERROR: "internal",
    HTTPStatus.SERVICE_UNAVAILABLE: "unavailable",
}


def default_code(status: int) -> str:
    return CODES.get(status) or f"http_{int(status)}"


class HttpError(Exception):
    """extra — дополнительные поля ответа рядом с error (например, existing у дубликата);
    headers — заголовки ответа (например, Allow у 405); code — машинный код ошибки,
    по умолчанию из статуса (docs/api.md)."""

    def __init__(
        self,
        status: HTTPStatus,
        message: str,
        extra: dict | None = None,
        headers: dict[str, str] | None = None,
        *,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra or {}
        self.headers = headers or {}
        self.code = code or default_code(status)

    def body(self) -> dict:
        """Тело ответа: {"error", "code", …extra}. error — для человека, code — для программы."""
        return {"error": self.message, "code": self.code, **self.extra}


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
        self.started = False  # ответ уже начат: второй (об ошибке) слать нельзя

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
        if length < 0:  # rfile.read(-1) читал бы до EOF и вешал keep-alive
            self._h.close_connection = True
            raise bad_request("Неверный заголовок Content-Length.")
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

    def send_json(
        self, payload: dict, status: HTTPStatus = HTTPStatus.OK, headers: dict[str, str] | None = None
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h = self._h
        self.started = True
        h.send_response(status)
        h.send_header("Content-Type", "application/json; charset=utf-8")
        h.send_header("Content-Length", str(len(data)))
        h.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            h.send_header(name, value)
        h.end_headers()
        if not self.head:
            h.wfile.write(data)

    def redirect(self, location: str) -> None:
        h = self._h
        self.started = True
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
        stat = path.stat()
        size = stat.st_size
        start, end, status = 0, size - 1, HTTPStatus.OK
        header = self.headers.get("Range")
        self.started = True
        # Валидаторы (FE-006): с ними no-cache превращается в дешёвый 304 без тела при каждом переходе.
        etag = f'"{stat.st_mtime_ns:x}-{size:x}"'
        modified = formatdate(stat.st_mtime, usegmt=True)
        if not_modified(self.headers, etag, stat.st_mtime):
            h.send_response(HTTPStatus.NOT_MODIFIED)
            h.send_header("ETag", etag)
            h.send_header("Last-Modified", modified)
            h.send_header("Cache-Control", cache)
            h.end_headers()
            return
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
        h.send_header("ETag", etag)
        h.send_header("Last-Modified", modified)
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


def not_modified(headers, etag: str, mtime: float) -> bool:
    """Условный GET: If-None-Match (главнее) или If-Modified-Since совпали с файлом → 304."""
    match = headers.get("If-None-Match")
    if match is not None:
        tags = {t.strip().removeprefix("W/") for t in match.split(",")}
        return "*" in tags or etag in tags
    since = headers.get("If-Modified-Since")
    if not since:
        return False
    try:
        return int(mtime) <= parsedate_to_datetime(since).timestamp()
    except (TypeError, ValueError, IndexError, OverflowError):
        return False


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
            req.send_json(body, HTTPStatus.CREATED, {"Location": body["url"]})
            return True
        case ("GET", ["uploads", upload_id]):
            path = store.path(upload_id)
            if path is None:
                raise not_found("Файл не найден.")
            req.send_file(path, MIME[path.suffix[1:]], cache="private, max-age=86400", ranged=True)
            return True
    return False
