"""Контракт HTTP API (BE-011, docs/api.md): у каждой ошибки есть code, коды и глаголы едины."""

import json
import unittest
from http import HTTPStatus

from aiplatform.core import narrator
from aiplatform.core.http import HttpError, default_code
from tests.helpers import PlatformCase

# Пути, которые есть или похожи на настоящие у каждой вкладки: несуществующий id, чужой подпуть.
# Тело — битый JSON: ручка с телом отвечает 400 до любой работы, без тела — 404 на чужой id.
PROBES = ("__nope__", "jobs/nope", "jobs/nope/cancel", "projects/nope", "projects/nope/run", "items/nope")


class HttpErrorCodeTest(unittest.TestCase):
    def test_code_from_status_and_override(self):
        self.assertEqual(HttpError(HTTPStatus.NOT_FOUND, "нет").body(), {"error": "нет", "code": "not_found"})
        self.assertEqual(HttpError(HTTPStatus.CONFLICT, "x").code, "conflict")
        self.assertEqual(HttpError(HTTPStatus.IM_A_TEAPOT, "x").code, "http_418")
        err = HttpError(HTTPStatus.BAD_REQUEST, "x", {"existing": "p1"}, code="duplicate")
        self.assertEqual(err.body(), {"error": "x", "code": "duplicate", "existing": "p1"})
        self.assertEqual(default_code(503), "unavailable")

    def test_narrator_kind_keeps_kind_and_gets_code(self):
        err = narrator.http_error(narrator.NarratorError("нет движка"))
        self.assertEqual(err.body(), {"error": "нет движка", "code": "engine_unavailable", "kind": "unavailable"})


class ErrorsHaveCodeTest(PlatformCase):
    """Каждый ответ-ошибка — JSON с error и code: по реестру вкладок и на уровне сервера."""

    def check(self, method, path, body=None, headers=None):
        status, got_headers, raw = self.call(method, path, body, headers)
        if status < 400:
            return status
        self.assertIn("application/json", got_headers.get("Content-Type", ""), (method, path, status))
        data = json.loads(raw)
        self.assertIsInstance(data.get("error"), str, (method, path, status, data))
        self.assertIsInstance(data.get("code"), str, (method, path, status, data))
        self.assertTrue(data["code"], (method, path))
        return status

    def test_every_module_error_has_code(self):
        self.assertTrue(self.app.modules)
        for prefix in self.app.modules:
            errors = 0
            for sub in PROBES:
                with self.subTest(prefix=prefix, sub=sub):
                    errors += self.check("GET", f"/api/{prefix}/{sub}") >= 400
                    errors += self.check("POST", f"/api/{prefix}/{sub}", b"{") >= 400
            self.assertGreater(errors, 0, prefix)

    def test_server_level_errors_have_code(self):
        self.assertEqual(self.check("GET", "/api/nope"), 404)
        self.assertEqual(self.check("PUT", "/api/status"), 405)
        self.assertEqual(self.check("GET", "/api/status", headers={"Host": "evil.example"}), 421)
        self.assertEqual(self.check("POST", "/api/studio/projects", {}, {"X-AIP": None}), 403)
        self.assertEqual(self.check("POST", "/api/photo/jobs", b"{"), 400)

    def test_stopping_and_not_ready_have_code(self):
        self.app.stopping = True
        status, body = self.json("POST", "/api/photo/jobs", {})
        self.assertEqual((status, body["code"]), (503, "stopping"))
        status, body = self.json("GET", "/api/ready")
        self.assertEqual((status, body["code"]), (503, "not_ready"))


class VerbsAndStatusesTest(PlatformCase):
    def test_created_has_location(self):
        status, headers, raw = self.call("POST", "/api/photo/jobs", {"prompt": "кот", "width": 512, "height": 512})
        self.assertEqual(status, 201, raw)
        self.assertEqual(headers["Location"], f"/api/photo/jobs/{json.loads(raw)['id']}")

    def test_channels_items_and_alias(self):
        body = {"label": "Мой", "chat_id": "@my_chan", "format": "vertical"}
        status, headers, raw = self.call("POST", "/api/channels/items", body)
        self.assertEqual(status, 201, raw)
        added = json.loads(raw)["added"]
        self.assertEqual(headers["Location"], f"/api/channels/items/{added}")
        # Старый адрес — алиас того же ресурса.
        status, data = self.json("POST", f"/api/channels/channels/{added}", {"label": "Другой"})
        self.assertEqual(status, 200, data)
        status, data = self.json("POST", "/api/channels/items/nope", {"label": "x"})
        self.assertEqual((status, data["code"]), (404, "not_found"))

    def test_broken_channels_file_is_503(self):
        (self.config.data_dir / "channels.json").write_text("{битый")
        status, data = self.json("POST", "/api/channels/items", {"label": "x", "chat_id": "@x"})
        self.assertEqual((status, data["code"]), (503, "config_broken"), data)


if __name__ == "__main__":
    unittest.main()
