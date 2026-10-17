"""Лимиты загрузки через HTTP: 413 с размером в МБ, пустое тело, неверный Content-Length (QA-015).

Лимиты понижены патчем (настоящие — 25 и 200 МБ), тело сверх лимита не отправляется вовсе:
ответ 413 приходит по одному заголовку, значит сервер не читает тело в память.
"""

import http.client
import json
import socket
import time
from unittest import mock

from aiplatform.core import uploads
from tests.fixtures import ARTICLE, PNG_STUB
from tests.helpers import StudioCase, scaled

MB = 2**20
IMAGE_LIMIT = 1 * MB
AUDIO_LIMIT = 2 * MB
MP3 = b"ID3\x04" + b"\x00" * 60


class UploadLimitsTest(StudioCase):
    def setUp(self):
        super().setUp()
        for name, value in (("MAX_UPLOAD_BYTES", IMAGE_LIMIT), ("MAX_AUDIO_BYTES", AUDIO_LIMIT)):
            patcher = mock.patch.object(uploads, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def send(self, path, body=b"", *, length=None, hold=False):
        """POST сырым сокетом: свой Content-Length; hold — не закрывать запись (клиент «замолчал»).

        Возвращает (статус, JSON-ответ, секунды до ответа)."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=scaled(5))
        self.addCleanup(sock.close)
        size = len(body) if length is None else length
        head = (
            f"POST {path} HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nX-AIP: 1\r\n"
            f"Content-Type: application/octet-stream\r\nContent-Length: {size}\r\n\r\n"
        )
        started = time.monotonic()
        sock.sendall(head.encode() + body)
        if not hold:
            sock.shutdown(socket.SHUT_WR)
        resp = http.client.HTTPResponse(sock)
        resp.begin()
        payload = json.loads(resp.read() or b"{}")
        resp.close()
        return resp.status, payload, time.monotonic() - started

    def project(self) -> str:
        return self.create(scenario="article", text=ARTICLE)["id"]

    # --- POST …/uploads модулей ---

    def test_module_upload_over_limit_is_413_without_reading_body(self):
        cases = (
            ("/api/photo/uploads", IMAGE_LIMIT, "Файл больше 1 МБ."),
            ("/api/video/uploads", IMAGE_LIMIT, "Файл больше 1 МБ."),
            ("/api/audio/uploads", AUDIO_LIMIT, "Файл больше 2 МБ."),
        )
        for path, limit, text in cases:
            with self.subTest(path):
                # Тело не шлём вовсе: ответ по одному заголовку — сервер тело не ждёт и не читает.
                status, body, _ = self.send(path, length=limit + 1, hold=True)
                self.assertEqual((status, body["code"]), (413, "too_large"), body)
                self.assertEqual(body["error"], text)

    def test_module_upload_at_limit_is_accepted(self):
        status, body, _ = self.send("/api/photo/uploads", PNG_STUB + b"\x00" * (IMAGE_LIMIT - len(PNG_STUB)))
        self.assertEqual(status, 201, body)
        status, body, _ = self.send("/api/audio/uploads", MP3)
        self.assertEqual(status, 201, body)

    def test_empty_body_is_400_with_text(self):
        for path in ("/api/photo/uploads", "/api/audio/uploads"):
            with self.subTest(path):
                status, body, _ = self.send(path, b"")
                self.assertEqual((status, body["error"]), (400, "Пустой файл."), body)

    def test_content_length_shorter_than_body_is_an_error_not_a_hang(self):
        # Сервер читает ровно Content-Length: 4 байта PNG — не картинка, ответ сразу.
        status, body, seconds = self.send("/api/photo/uploads", PNG_STUB, length=4)
        self.assertEqual(status, 400, body)
        self.assertIn("PNG", body["error"])
        self.assertLess(seconds, scaled(2))

    def test_body_cut_short_is_400(self):
        # Content-Length больше, чем пришло, клиент закрыл запись — обрыв посреди тела.
        status, body, seconds = self.send("/api/photo/uploads", PNG_STUB, length=1000)
        self.assertEqual(status, 400, body)
        self.assertIn("оборвал", body["error"])
        self.assertLess(seconds, scaled(2))

    def test_silent_client_is_dropped_by_socket_timeout(self):
        # Клиент прислал часть тела и замолчал, соединение держит: поток сервера не висит вечно.
        self.server.RequestHandlerClass.timeout = 0.3  # у каждого сервера свой класс обработчика
        status, body, seconds = self.send("/api/photo/uploads", PNG_STUB, length=1000, hold=True)
        self.assertEqual(status, 400, body)
        self.assertIn("оборвал", body["error"])
        self.assertLess(seconds, scaled(2))

    # --- студия: постер и голос ---

    def test_studio_poster_over_image_limit_is_413(self):
        pid = self.project()
        big = PNG_STUB + b"\x00" * IMAGE_LIMIT
        status, body, _ = self.send(f"/api/studio/projects/{pid}/upload/poster", big)
        self.assertEqual((status, body["error"]), (413, "Картинка больше 1 МБ."), body)

    def test_studio_voice_over_audio_limit_is_413(self):
        pid = self.project()
        status, body, _ = self.send(f"/api/studio/projects/{pid}/upload/voice", length=AUDIO_LIMIT + 1, hold=True)
        self.assertEqual((status, body["error"]), (413, "Файл больше 2 МБ."), body)

    def test_studio_empty_upload_is_400(self):
        pid = self.project()
        for what in ("poster", "voice"):
            with self.subTest(what):
                status, body, _ = self.send(f"/api/studio/projects/{pid}/upload/{what}", b"")
                self.assertEqual((status, body["error"]), (400, "Пустой файл."), body)
