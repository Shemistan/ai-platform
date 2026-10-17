"""Лимиты памяти и потоков (SEC-009): загрузки потоком на диск, потолок загрузок и обработчиков,
ограничение ответов внешних страниц."""

import io
import socket
import tempfile
import threading
import time
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from aiplatform.core import http, uploads
from aiplatform.studio import stats
from tests.fixtures import png
from tests.helpers import PlatformCase


class CountingReader(io.RawIOBase):
    """rfile: отдаёт size байт и запоминает, сколько просили за раз."""

    def __init__(self, size: int, payload: bytes = b"\x89PNG\r\n\x1a\n", cut: int | None = None):
        self.size, self.payload, self.cut = size, payload, cut
        self.pos, self.asked = 0, []

    def read(self, n=-1):
        self.asked.append(n)
        end = self.size if self.cut is None else self.cut
        if n is None or n < 0:
            n = end - self.pos
        n = max(min(n, end - self.pos), 0)
        if not n:
            return b""
        head = self.payload[self.pos : self.pos + n] if self.pos < len(self.payload) else b""
        out = head + b"\0" * (n - len(head))
        self.pos += n
        return out


def request(length: int, rfile) -> http.Request:
    handler = mock.Mock()
    handler.headers = {"Content-Length": str(length)}
    handler.rfile = rfile
    handler.close_connection = False
    return http.Request(handler, "POST", ["uploads"], {})


class BodyToFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_too_big_rejected_without_reading_body(self):
        rfile = CountingReader(300 * 2**20)
        with self.assertRaises(http.HttpError) as caught:
            request(300 * 2**20, rfile).body_to_file(self.dir, 200 * 2**20)
        self.assertEqual(caught.exception.status, HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.assertEqual(rfile.asked, [], "тело не читалось")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_streams_in_chunks(self):
        size = 5 * http.CHUNK + 123
        rfile = CountingReader(size)
        tmp = request(size, rfile).body_to_file(self.dir, 200 * 2**20)
        self.assertEqual(tmp.stat().st_size, size)
        self.assertTrue(all(0 < n <= http.CHUNK for n in rfile.asked), rfile.asked)
        self.assertEqual(tmp.stat().st_mode & 0o077, 0, "загрузка — только владельцу")

    def test_broken_upload_leaves_no_temp_file(self):
        rfile = CountingReader(10 * http.CHUNK, cut=3 * http.CHUNK)
        with self.assertRaises(http.HttpError) as caught:
            request(10 * http.CHUNK, rfile).body_to_file(self.dir, 200 * 2**20)
        self.assertEqual(caught.exception.status, HTTPStatus.BAD_REQUEST)
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_upload_slots(self):
        taken = [http._upload_slots.acquire(blocking=False) for _ in range(http.UPLOAD_SLOTS)]
        self.assertTrue(all(taken))
        try:
            with self.assertRaises(http.HttpError) as caught:
                request(10, CountingReader(10)).body_to_file(self.dir, 100)
            self.assertEqual(caught.exception.status, HTTPStatus.SERVICE_UNAVAILABLE)
        finally:
            for _ in taken:
                http._upload_slots.release()
        request(10, CountingReader(10)).body_to_file(self.dir, 100)  # слоты вернулись

    def test_store_rejects_bad_file_and_cleans_up(self):
        store = uploads.UploadStore(self.dir / "store")
        bad = self.dir / "store" / ".upload-x.part"
        bad.write_bytes(b"hello")
        with self.assertRaises(uploads.UploadError):
            store.save_file(bad, (uploads.IMAGE,))
        self.assertFalse(bad.exists())
        good = self.dir / "store" / ".upload-y.part"
        good.write_bytes(png(8, 8))
        upload_id = store.save_file(good, (uploads.IMAGE,))
        self.assertEqual(store.path(upload_id).suffix, ".png")
        self.assertEqual(sorted(p.name for p in (self.dir / "store").iterdir()), [f"{upload_id}.png"])


class UploadServerTest(PlatformCase):
    def test_upload_works_and_leaves_no_parts(self):
        status, body = self.json("POST", "/api/photo/uploads", png(64, 48))
        self.assertEqual(status, 201, body)
        self.assertEqual((body["width"], body["height"]), (64, 48))
        status, _ = self.json("POST", "/api/photo/uploads", b"not an image")
        self.assertEqual(status, 400)
        parts = list(self.config.uploads_dir.glob(".upload-*"))
        self.assertEqual(parts, [], "временные файлы загрузок удалены")


class HandlerCapTest(PlatformCase):
    def test_connections_over_cap_get_503(self):
        self.server._slots = threading.BoundedSemaphore(1)
        held = socket.create_connection(("127.0.0.1", self.port))  # держит единственный слот
        self.addCleanup(held.close)
        time.sleep(0.2)
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as extra:
            extra.sendall(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
            answer = extra.recv(4096)
        self.assertTrue(answer.startswith(b"HTTP/1.1 503"), answer[:80])
        held.close()
        time.sleep(0.3)
        status, _ = self.json("GET", "/api/health")
        self.assertEqual(status, 200, "слот вернулся после закрытия соединения")


class ClientLimitTest(unittest.TestCase):
    def test_stats_page_over_limit_is_empty(self):
        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        big = Resp(b"x" * (stats.MAX_PAGE_BYTES + 10))
        with mock.patch("urllib.request.urlopen", return_value=big):
            self.assertEqual(stats.http_get("https://t.me/x/1?embed=1"), "")
        with mock.patch("urllib.request.urlopen", return_value=Resp(b"<b>12K views</b>")):
            self.assertEqual(stats.http_get("https://t.me/x/1?embed=1"), "<b>12K views</b>")


if __name__ == "__main__":
    unittest.main()
