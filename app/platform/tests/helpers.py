"""Общая обвязка тестов: платформа на фейковых бэкендах в отдельном потоке."""

import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from aiplatform import config as config_module
from aiplatform import server as server_module


class PlatformCase(unittest.TestCase):
    """Сервер на порту 0 с фейковыми бэкендами; modules — какие вкладки поднять."""

    extra_env: dict[str, str] = {}
    process_env: dict[str, str] = {}  # то, что читают фейковые CLI в подпроцессах

    @staticmethod
    def modules(ctx):
        return server_module.default_modules(ctx)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for key, value in self.process_env.items():
            old = os.environ.get(key)
            os.environ[key] = value
            if old is None:
                self.addCleanup(os.environ.pop, key, None)
            else:
                self.addCleanup(os.environ.__setitem__, key, old)
        env = {"AIP_BACKEND": "fake", "AIP_DATA_DIR": self.tmp.name, "AIP_PORT": "0", **self.extra_env}
        self.config = config_module.load(env)
        self.server, self.app = server_module.make_server(self.config, type(self).modules)
        self.port = self.server.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop, thread)

    def _stop(self, thread):
        self.server.shutdown()
        self.server.server_close()
        self.app.close()
        thread.join(timeout=5)

    def call(self, method, path, body=None, headers=None):
        data = body if isinstance(body, bytes | None) else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    def json(self, method, path, body=None, headers=None):
        status, _, raw = self.call(method, path, body, headers)
        return status, json.loads(raw)

    def raw(self, method, path):
        """Без перехода по редиректам: (статус, заголовки)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            resp.read()
            return resp.status, resp.headers
        finally:
            conn.close()

    def poll(self, path, done, timeout=30, interval=0.05):
        """GET path, пока done(ответ) не станет истинным; возвращает последний ответ."""
        deadline = time.time() + timeout
        body = None
        while time.time() < deadline:
            _, body = self.json("GET", path)
            if done(body):
                return body
            time.sleep(interval)
        self.fail(f"не дождались {path}: {body}")
