"""Оболочка: редиректы, конфиг, статус GPU, защита от чужого Origin, Host и токен доступа."""

import http.client
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

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


class NavTabsTest(PlatformCase):
    """Шапка из /api/config (FE-007): каждый модуль сервера есть в навигации, группы не рвутся."""

    def test_every_module_in_nav(self):
        status, body = self.json("GET", "/api/config")
        self.assertEqual(status, 200)
        tabs = body["tabs"]
        paths = {t["href"].split("?")[0] for t in tabs}
        for prefix in self.app.modules:
            self.assertIn(f"/{prefix}/", paths, prefix)
        ids = [t["id"] for t in tabs]
        self.assertEqual(len(ids), len(set(ids)))
        groups = [g for i, g in enumerate(t["group"] for t in tabs) if not i or tabs[i - 1]["group"] != g]
        self.assertEqual(len(groups), len(set(groups)), "группа вкладок разорвана другой")
        # Три раздела (UX-037): Выпуски (доска и сценарии) первыми, потом Инструменты.
        self.assertEqual(ids[:5], ["board", "digest", "article", "post", "photo"])
        self.assertEqual([t["id"] for t in tabs if t.get("home")], ["board"])
        self.assertEqual({t["group_icon"] for t in tabs if t.get("group_icon")}, {"settings"})
        self.assertEqual([t["id"] for t in tabs if t.get("default")], ["article"])
        self.assertEqual([t["badge"] for t in tabs if t.get("badge")], ["attention", "advice"])
        labels = {t["group"]: t["group_label"] for t in tabs}
        want = {"releases": "Выпуски", "tools": "Инструменты", "texts": "Тексты", "service": "Сервис"}
        self.assertEqual(labels, want)

    def test_advice_not_in_problems(self):
        """UX-030: открытый каталог данных — совет у «Настроек», не баннер на всех страницах и не 503 в /ready."""
        tip = f"Каталог данных {self.tmp.name} доступен другим пользователям машины — chmod 700 '{self.tmp.name}'."
        with (
            mock.patch.object(type(self.config), "fake", new_callable=mock.PropertyMock, return_value=False),
            mock.patch.object(server_module.perms, "warning", return_value=tip),
        ):
            self.assertEqual(self.app.advice(), [tip])
            self.assertNotIn(tip, self.app.problems())
        _, body = self.json("GET", "/api/config")
        self.assertEqual(body["advice"], [])  # в демо права каталога не проверяются

    def test_activity_in_status(self):
        """UX-036: /api/status → activity — идущие задачи всех вкладок со ссылкой; готовые не попадают."""
        photo = self.app.modules["photo"]
        jobs = [
            {"status": "running", "prompt": "кот  в\nшляпе", "progress": {"stage": "Шаг 3/9", "percent": 33.0}},
            {"status": "done", "prompt": "готово", "progress": {"stage": "Готово", "percent": 100.0}},
        ]
        montage = self.app.modules["montage"]
        queued = [{"id": "m1", "title": "Обзор", "status": "queued"}]
        with (
            mock.patch.object(photo.jobs, "list", return_value=jobs),
            mock.patch.object(montage.projects, "active", return_value=queued),
        ):
            _, body = self.json("GET", "/api/status")
        found = {(a["tab"], a["href"], a["title"], a["status"], a["stage"], a["percent"]) for a in body["activity"]}
        self.assertIn(("Фото", "/photo/", "кот в шляпе", "running", "Шаг 3/9", 33.0), found)
        self.assertIn(("Монтаж", "/montage/?id=m1", "Обзор", "queued", "", None), found)
        self.assertFalse([a for a in body["activity"] if a["title"] == "готово"])

    def test_upload_limits_in_module_config(self):
        """FE-003: страница сверяет размер файла с лимитом до отправки — лимиты в /config модулей."""
        from aiplatform.core import uploads

        want = {"image": uploads.MAX_UPLOAD_BYTES, "media": uploads.MAX_AUDIO_BYTES}
        for module in ("photo", "video", "audio", "avatar", "studio"):
            status, body = self.json("GET", f"/api/{module}/config")
            self.assertEqual(status, 200, module)
            self.assertEqual(body["upload_limits"], want, module)


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
        self.assertIn(b"/api/config", body)
        self.assertNotIn("Видео по статье".encode(), body, "вкладки шапки — только с сервера (FE-007)")
        status, _, _ = self.call("GET", "/../server.py")
        self.assertEqual(status, 404)

    def test_static_etag_and_304(self):
        """FE-006: статика с ETag и Last-Modified; повтор с валидатором — 304 без тела."""
        status, headers, body = self.call("GET", "/nav.js")
        self.assertEqual(status, 200)
        etag, modified = headers["ETag"], headers["Last-Modified"]
        self.assertTrue(etag.startswith('"') and body)
        self.assertEqual(headers["Cache-Control"], "no-cache")
        for cond in ({"If-None-Match": etag}, {"If-None-Match": f'"x", W/{etag}'}, {"If-Modified-Since": modified}):
            status, headers, body = self.call("GET", "/nav.js", headers=cond)
            self.assertEqual((status, body), (304, b""), cond)
            self.assertEqual(headers["ETag"], etag)
        status, _, body = self.call("GET", "/nav.js", headers={"If-None-Match": '"old"'})
        self.assertEqual(status, 200)
        self.assertTrue(body)
        # If-None-Match главнее If-Modified-Since: другой тег — полный ответ.
        status, _, _ = self.call("GET", "/nav.js", headers={"If-None-Match": '"old"', "If-Modified-Since": modified})
        self.assertEqual(status, 200)

    def test_favicon(self):
        status, headers, body = self.call("GET", "/favicon.ico")
        self.assertEqual(status, 200)
        self.assertIn("svg", headers["Content-Type"])
        self.assertIn(b"<svg", body)
        pages = sorted(server_module.STATIC_DIR.glob("*/index.html"))
        self.assertTrue(pages)
        for page in pages:
            self.assertIn('rel="icon" href="/favicon.svg"', page.read_text(encoding="utf-8"), page)

    def test_config_lists_tabs_and_problems(self):
        status, body = self.json("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertEqual(body["backend"], "fake")
        self.assertEqual(
            body["tabs"], [{"group": "echo", "id": "echo", "label": "Эхо", "href": "/echo/", "group_label": "Эхо"}]
        )
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
        self.assertIn(b"/api/config", body)
        self.assertNotIn("Видео по статье".encode(), body, "вкладки шапки — только с сервера (FE-007)")
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
