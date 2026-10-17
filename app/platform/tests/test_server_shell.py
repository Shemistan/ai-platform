"""Оболочка: редиректы, конфиг, статус GPU, защита от чужого Origin, Host и токен доступа."""

import http.client
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from aiplatform import config as config_module
from aiplatform import server as server_module
from aiplatform.core import narrator
from aiplatform.core.http import found, not_found
from tests.helpers import PlatformCase, isolated_config

AUTH = "test-token-" + "x" * 16
SECURITY = ("X-Frame-Options", "X-Content-Type-Options", "Referrer-Policy", "Content-Security-Policy")


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
        self.assertEqual(body["request_id"], logs.records[0].request_id)
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
        conn.putheader("X-AIP", "1")  # иначе 403 раньше разбора тела (SEC-002)
        conn.putheader("Content-Length", "-1")
        conn.endheaders()
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        self.assertIn("Content-Length", json.loads(resp.read())["error"])

    def test_close_closes_modules(self):
        module = self.app.modules["echo"]
        self.app.close()
        self.assertTrue(module.closed)


class EchoCase(PlatformCase):
    @staticmethod
    def modules(ctx):
        return [EchoModule(ctx)]

    def request(self, method, path, headers=None, body=None):
        """Сырой запрос с любыми заголовками, в том числе Host; без редиректов."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers={"Host": f"127.0.0.1:{self.port}", **(headers or {})})
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()


class HostOriginHeadersTest(EchoCase):
    """SEC-002: DNS rebinding, CSRF без Origin, clickjacking."""

    def test_foreign_host_rejected(self):
        status, _, body = self.request("GET", "/api/config", {"Host": f"attacker.example:{self.port}"})
        self.assertEqual(status, 421, body)
        self.assertEqual(self.request("GET", "/photo/", {"Host": "attacker.example"})[0], 421)
        for name in ("127.0.0.1", "localhost", "LOCALHOST", "[::1]"):
            host = f"{name}:{self.port}"
            with self.subTest(host=host):
                self.assertEqual(self.request("GET", "/api/config", {"Host": host})[0], 200)

    def test_post_needs_origin_or_service_header(self):
        status, _ = self.json("POST", "/api/echo/hello", {"a": 1}, {"X-AIP": None})
        self.assertEqual(status, 403)
        self.assertEqual(self.json("POST", "/api/echo/hello", {"a": 1})[0], 201)  # X-AIP: 1
        ok = {"X-AIP": None, "Origin": f"http://localhost:{self.port}"}
        self.assertEqual(self.json("POST", "/api/echo/hello", {"a": 1}, ok)[0], 201)
        for origin in ("null", "http://evil.example", f"http://evil.example:{self.port}", "file://"):
            with self.subTest(origin=origin):
                status, _ = self.json("POST", "/api/echo/hello", {"a": 1}, {"Origin": origin})
                self.assertEqual(status, 403)

    def test_security_headers_everywhere(self):
        for path in ("/api/config", "/nav.js", "/", "/api/nope", "/photo/"):
            with self.subTest(path=path):
                _, headers, _ = self.request("GET", path)
                for name in SECURITY:
                    self.assertIn(name, headers)
                self.assertEqual(headers["X-Frame-Options"], "DENY")
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertIn("script-src 'self'", headers["Content-Security-Policy"])
        _, headers, _ = self.request("HEAD", "/api/config")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_allowed_hosts_from_config(self):
        env = isolated_config({"AIP_ALLOWED_HOSTS": "Box.local, 10.0.0.5:9000,::1"})
        hosts = server_module.allowed_hosts(config_module.load(env), 8780)
        self.assertLessEqual(
            {"127.0.0.1:8780", "localhost:8780", "[::1]:8780", "box.local:8780", "10.0.0.5:9000"}, hosts
        )
        self.assertNotIn("attacker.example:8780", hosts)


class AuthTest(EchoCase):
    """SEC-007: при AIP_AUTH_TOKEN без токена — 401, с токеном — 200."""

    extra_env = {"AIP_AUTH_TOKEN": AUTH}

    def test_api_needs_token(self):
        status, _, body = self.request("GET", "/api/config")
        self.assertEqual(status, 401, body)
        self.assertEqual(self.request("GET", "/api/config", {"Authorization": "Bearer wrong-token"})[0], 401)
        self.assertEqual(self.request("GET", "/api/config", {"Authorization": f"Bearer {AUTH}"})[0], 200)
        self.assertEqual(self.request("GET", "/api/config", {"Cookie": f"aip_auth={AUTH}"})[0], 200)
        self.assertEqual(self.request("GET", "/api/health")[0], 200)  # живость — без токена

    def test_login_link_sets_cookie(self):
        status, headers, body = self.request("GET", "/nav.js")
        self.assertEqual(status, 401)
        self.assertIn(b'name="token"', body)
        self.assertEqual(self.request("GET", "/photo/?token=wrong")[0], 401)
        status, headers, _ = self.request("GET", f"/photo/?token={AUTH}&x=1")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "/photo/?x=1")
        cookie = headers["Set-Cookie"]
        for part in (f"aip_auth={AUTH}", "HttpOnly", "SameSite=Strict", "Path=/"):
            self.assertIn(part, cookie)
        status, _, body = self.request("GET", "/nav.js", {"Cookie": f"aip_auth={AUTH}"})
        self.assertEqual(status, 200)
        self.assertIn(b"TABS", body)
        post = {"Authorization": f"Bearer {AUTH}", "X-AIP": "1", "Content-Type": "application/json"}
        self.assertEqual(self.request("POST", "/api/echo/hello", post, b"{}")[0], 201)
        self.assertEqual(self.request("POST", "/api/echo/hello", {"X-AIP": "1"}, b"{}")[0], 401)


class BindConfigTest(unittest.TestCase):
    """SEC-007: не-loopback адрес без токена — сервер не стартует."""

    def test_non_loopback_needs_token(self):
        for host in ("0.0.0.0", "192.168.1.10", "::"):
            with self.subTest(host=host), self.assertRaisesRegex(config_module.ConfigError, "AIP_AUTH_TOKEN"):
                config_module.load(isolated_config({"AIP_HOST": host}))
        for host in ("127.0.0.1", "localhost", "::1", ""):
            with self.subTest(host=host):
                config = config_module.load(isolated_config({"AIP_HOST": host}))
                self.assertEqual(config.auth_token, "")
        config = config_module.load(isolated_config({"AIP_HOST": "0.0.0.0", "AIP_AUTH_TOKEN": AUTH}))
        self.assertEqual(config.auth_token, AUTH)
        with self.assertRaisesRegex(config_module.ConfigError, "16"):
            config_module.load(isolated_config({"AIP_AUTH_TOKEN": "short"}))

    def test_server_exits_with_message(self):
        env = os.environ | isolated_config({"AIP_HOST": "0.0.0.0", "AIP_PORT": "0", "AIP_AUTH_TOKEN": ""})
        done = subprocess.run(
            [sys.executable, "-m", "aiplatform.server"],
            cwd=Path(__file__).resolve().parents[1],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("AIP_AUTH_TOKEN", done.stderr)
        self.assertIn("не запущен", done.stderr)


if __name__ == "__main__":
    unittest.main()
