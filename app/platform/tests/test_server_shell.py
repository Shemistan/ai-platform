"""Оболочка: редиректы, конфиг, статус GPU, защита от чужого Origin."""

import http.client
import json
import unittest

from aiplatform.core import narrator
from aiplatform.core.http import found, not_found
from tests.helpers import PlatformCase


class EchoModule:
    """Минимальный модуль для проверки маршрутизации."""

    prefix = "echo"
    label = "Эхо"

    def __init__(self, ctx):
        self.ctx = ctx
        self.closed = False

    def handle(self, req):
        match (req.method, req.sub):
            case ("GET", ["hello"]):
                return req.send_json({"sub": req.sub, "q": req.arg("q")})
            case ("POST", ["hello"]):
                return req.send_json({"got": req.json()}, 201)
            case ("GET", ["missing"]):
                found(None)
            case ("GET", ["boom"]):
                return {}["нет ключа"]  # баг в модуле
        raise not_found()

    def describe(self):
        return {}

    def problems(self):
        return ["эхо не настроено"]

    def close(self):
        self.closed = True


class IncompatibleNarratorTest(PlatformCase):
    """Движок с чужой версией моста → один баннер в /api/config, а не тишина."""

    process_env = {"FAKE_BRIDGE_API": "7"}

    def setUp(self):
        narrator.reset_checks()
        self.addCleanup(narrator.reset_checks)
        super().setUp()

    def test_config_reports_narrator_once(self):
        status, body = self.json("GET", "/api/config")
        self.assertEqual(status, 200)
        found_ = [p for p in body["problems"] if "версия моста 7" in p]
        self.assertEqual(len(found_), 1, body["problems"])
        self.assertIn("AIP_NARRATOR_DIR", found_[0])


class ShellTest(PlatformCase):
    @staticmethod
    def modules(ctx):
        return [EchoModule(ctx)]

    def test_root_redirects_to_photo(self):
        status, headers = self.raw("GET", "/")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/photo/")

    def test_static_shell(self):
        status, headers, body = self.call("GET", "/nav.js")
        self.assertEqual(status, 200)
        self.assertIn("javascript", headers["Content-Type"])
        self.assertIn(b"TABS", body)
        status, _, _ = self.call("GET", "/../server.py")
        self.assertEqual(status, 404)

    def test_config_lists_tabs_and_problems(self):
        status, body = self.json("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["backend"], "fake")
        self.assertEqual(body["tabs"], [{"id": "echo", "label": "Эхо"}])
        self.assertIn("эхо не настроено", body["problems"])

    def test_status(self):
        status, body = self.json("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertEqual(body, {"gpu": None, "waiting": 0})

    def test_module_routing(self):
        status, body = self.json("GET", "/api/echo/hello?q=1")
        self.assertEqual((status, body), (200, {"sub": ["hello"], "q": "1"}))
        status, body = self.json("POST", "/api/echo/hello", {"a": 1})
        self.assertEqual((status, body), (201, {"got": {"a": 1}}))
        status, body = self.json("GET", "/api/echo/missing")
        self.assertEqual(status, 404)
        self.assertEqual(body["error"], "Задача не найдена.")
        status, _ = self.json("GET", "/api/nope")
        self.assertEqual(status, 404)

    def test_bad_json(self):
        status, body = self.json("POST", "/api/echo/hello", b"{not json")
        self.assertEqual(status, 400)
        self.assertIn("JSON", body["error"])

    def test_foreign_origin_rejected(self):
        status, body = self.json("POST", "/api/echo/hello", {"a": 1}, {"Origin": "http://evil.example"})
        self.assertEqual(status, 403)
        status, _ = self.json("POST", "/api/echo/hello", {"a": 1}, {"Origin": self.base})
        self.assertEqual(status, 201)

    def test_unhandled_error_is_json_500(self):
        with self.assertLogs("aiplatform", "ERROR") as logs:
            status, body = self.json("GET", "/api/echo/boom")
        self.assertEqual(status, 500)
        self.assertEqual(body["code"], "internal")
        self.assertEqual(body["error"], "Внутренняя ошибка")
        self.assertTrue(body["request_id"])
        self.assertIn(body["request_id"], logs.output[0])
        self.assertIn("KeyError", logs.output[0])

    def test_other_methods_get_json_405(self):
        for method in ("DELETE", "PUT", "PATCH"):
            for path in ("/api/echo/hello", "/api/config", "/nav.js"):
                status, headers, raw = self.call(method, path)
                self.assertEqual(status, 405, (method, path))
                self.assertIn("application/json", headers["Content-Type"])
                self.assertEqual(headers["Allow"], "GET, HEAD, POST")
                self.assertIn("error", json.loads(raw))
        status, headers, _ = self.call("POST", "/nav.js", b"")
        self.assertEqual((status, headers["Allow"]), (405, "GET, HEAD"))

    def test_negative_content_length_is_400(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/api/echo/hello")
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertIn("Content-Length", json.loads(resp.read())["error"])

    def test_close_closes_modules(self):
        module = self.app.modules["echo"]
        self.app.close()
        self.assertTrue(module.closed)


if __name__ == "__main__":
    unittest.main()
