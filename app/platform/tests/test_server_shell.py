"""Оболочка: редиректы, конфиг, статус GPU, защита от чужого Origin."""

import unittest

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
        raise not_found()

    def describe(self):
        return {}

    def problems(self):
        return ["эхо не настроено"]

    def close(self):
        self.closed = True


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

    def test_close_closes_modules(self):
        module = self.app.modules["echo"]
        self.app.close()
        self.assertTrue(module.closed)


if __name__ == "__main__":
    unittest.main()
