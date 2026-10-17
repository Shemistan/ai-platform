"""Фейковый Telegram Bot API на настоящем HTTP (QA-007): платформа шлёт запросы как в бою.

Локальный http.server на 127.0.0.1:0 в своём потоке; платформа ходит в него через
AIP_TELEGRAM_API. Каждый запрос разбирается (multipart или urlencoded) и пишется в calls:
метод, токен, поля, файлы (имя, тип, байты). Ответы по сценарию: ok по умолчанию,
fail(...) — 429 с retry_after, 5xx, 413, отказ ok=false или обрыв без ответа.

Использование:

    with FakeTelegram() as tg, mock.patch.dict(os.environ, tg.env()):
        ...
        tg.requests[0].fields["caption"]

Тот же сервер годится для e2e (QA-008): адрес — tg.base, сценарии — fail().
"""

from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from email.parser import BytesParser
from email.policy import HTTP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aiplatform.core.notify import TELEGRAM_API_ENV

CHAT_METHODS = ("getChat", "getChatMemberCount")


@dataclass
class Call:
    method: str
    token: str
    fields: dict[str, str]
    files: dict[str, tuple[str, str, bytes]]  # поле → (имя файла, Content-Type, байты)
    content_type: str
    accepted: bool = True  # пост принят (ответ ok)

    @property
    def chat(self) -> str:
        return self.fields.get("chat_id", "")


@dataclass
class Rule:
    method: str
    kind: str  # "429" | "500" | "413" | "refuse" | "drop"
    times: int  # сколько раз сработать; 0 — всегда
    chat: str | None = None
    retry_after: int = 1
    description: str = ""
    used: int = field(default=0)


def parse_body(content_type: str, body: bytes) -> tuple[dict[str, str], dict[str, tuple[str, str, bytes]]]:
    """Поля и файлы запроса: multipart/form-data (как publish.multipart) или urlencoded (notify)."""
    if content_type.startswith("multipart/form-data"):
        message = BytesParser(policy=HTTP).parsebytes(f"Content-Type: {content_type}\r\n\r\n".encode() + body)
        fields: dict[str, str] = {}
        files: dict[str, tuple[str, str, bytes]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            filename = part.get_filename()
            payload = part.get_payload(decode=True) or b""
            if filename is not None:
                files[name] = (filename, part.get_content_type(), payload)
            else:
                fields[name] = payload.decode("utf-8")
        return fields, files
    pairs = urllib.parse.parse_qsl(body.decode("utf-8"), keep_blank_values=True)
    return dict(pairs), {}


class FakeTelegram:
    def __init__(self, *, members: int = 10, chat: dict | None = None) -> None:
        self.requests: list[Call] = []
        self.members = members
        self.chat = chat or {}  # что вернёт getChat сверх id
        self._rules: list[Rule] = []
        self._lock = threading.Lock()
        self._message_id = 0
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # тише в выводе тестов
                pass

            def do_POST(self):  # noqa: N802 — имя метода BaseHTTPRequestHandler
                fake._handle(self)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    # --- жизненный цикл ---

    def start(self) -> FakeTelegram:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> FakeTelegram:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def env(self) -> dict[str, str]:
        """Окружение, при котором платформа ходит сюда, а не в api.telegram.org."""
        return {TELEGRAM_API_ENV: self.base}

    # --- сценарии ---

    def fail(
        self, method: str, kind: str, *, times: int = 1, chat: str | None = None, retry_after: int = 1, text: str = ""
    ) -> None:
        """Следующие times вызовов method (в чат chat, если задан) ответят сбоем kind; times=0 — всегда."""
        with self._lock:
            self._rules.append(Rule(method, kind, times, chat, retry_after, text))

    def clear(self) -> None:
        with self._lock:
            self._rules.clear()

    # --- выборки ---

    def sent(self, method: str | None = None, *, chat: str | None = None, accepted: bool | None = None) -> list[Call]:
        with self._lock:
            return [
                c
                for c in self.requests
                if (method is None or c.method == method)
                and (chat is None or c.chat == chat)
                and (accepted is None or c.accepted == accepted)
            ]

    def methods(self) -> list[str]:
        with self._lock:
            return [c.method for c in self.requests]

    # --- сервер ---

    def _rule(self, call: Call) -> Rule | None:
        with self._lock:
            for rule in self._rules:
                if rule.method != call.method or (rule.chat is not None and rule.chat != call.chat):
                    continue
                if rule.times and rule.used >= rule.times:
                    continue
                rule.used += 1
                return rule
        return None

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        length = int(h.headers.get("Content-Length") or 0)
        body = h.rfile.read(length)
        _, _, rest = h.path.partition("/bot")
        token, _, method = rest.partition("/")
        fields, files = parse_body(h.headers.get("Content-Type", ""), body)
        call = Call(method, token, fields, files, h.headers.get("Content-Type", ""))
        rule = self._rule(call)
        status, answer = 200, None
        if rule is None:
            answer = self._ok(call)
        elif rule.kind == "drop":  # запрос принят, ответа нет: Telegram мог опубликовать
            call.accepted = False
            with self._lock:
                self.requests.append(call)
            h.close_connection = True  # ни строки ответа: клиент получит обрыв соединения
            return
        elif rule.kind == "429":
            status = 429
            text = rule.description or f"Too Many Requests: retry after {rule.retry_after}"
            answer = {
                "ok": False,
                "error_code": 429,
                "description": text,
                "parameters": {"retry_after": rule.retry_after},
            }
        elif rule.kind == "refuse":
            status = 400
            answer = {"ok": False, "error_code": 400, "description": rule.description or "Bad Request: chat not found"}
        else:
            status = int(rule.kind)
            text = rule.description or {"413": "Request Entity Too Large"}.get(rule.kind, "Internal Server Error")
            answer = {"ok": False, "error_code": status, "description": text}
        if status != 200:
            call.accepted = False
        with self._lock:
            self.requests.append(call)
        data = json.dumps(answer, ensure_ascii=False).encode()
        h.send_response(status)
        h.send_header("Content-Type", "application/json")
        h.send_header("Content-Length", str(len(data)))
        h.end_headers()
        h.wfile.write(data)

    def _ok(self, call: Call) -> dict:
        if call.method == "getChatMemberCount":
            return {"ok": True, "result": self.members}
        if call.method == "getChat":
            return {"ok": True, "result": {"id": call.chat, "type": "channel", **self.chat}}
        with self._lock:
            self._message_id += 1
            message_id = self._message_id
        return {"ok": True, "result": {"message_id": message_id, "chat": {"id": call.chat}}}
