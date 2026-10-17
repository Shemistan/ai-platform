#!/usr/bin/env python3
"""Тесты tools/api_routes.py: python3 tools/test_api_routes.py"""
import contextlib
import io
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_routes  # noqa: E402

ROOT = api_routes.ROOT
PKG = os.path.join(ROOT, api_routes.PLATFORM)
# Ветка-маршрут: case ("GET"|"POST", [...]) — альтернативы пути через «] | [» дают по строке.
CASE = re.compile(r'^\s*case \("(GET|POST)", (.*)\):?')

SERVICE = '''PREFIX = "demo"


class DemoModule:
    def hello(self) -> dict:
        """Поздороваться."""
        return {}

    def handle(self, req) -> None:
        match (req.method, req.sub):
            # Список всего.
            case ("GET", ["items"]):
                return req.send_json({})
            case ("POST", ["items", item_id]):
                return req.send_json(self.hello(req.json()), HTTPStatus.CREATED)
            case ("GET", ["a"] | ["b"]):
                return req.send_json({})
            case (_, ["any"]):
                return None
'''


def code_cases():
    """Сколько маршрутов в коде по грубому счёту строк `case ("GET"/"POST", …)`."""
    files = [os.path.join(PKG, "server.py")]
    files += [os.path.join(PKG, d, "service.py") for d in sorted(os.listdir(PKG))
              if os.path.isfile(os.path.join(PKG, d, "service.py"))]
    total = 0
    for path in files:
        with open(path, encoding="utf-8") as f:
            for line in f:
                m = CASE.match(line)
                if m:
                    total += 1 + m.group(2).count("] | [")
    return total


class RealCodeTest(unittest.TestCase):
    def test_every_case_is_in_reference(self):
        self.assertEqual(len(api_routes.collect()), code_cases())

    def test_reference_is_fresh(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            code = api_routes.sync(check=True)
        self.assertEqual(code, 0, err.getvalue())

    def test_known_routes_and_bridge(self):
        paths = {(r.method, r.path) for r in api_routes.collect()}
        for route in (("GET", "/api/config"), ("GET", "/api/status"), ("GET", "/api/health"),
                      ("GET", "/api/ready"), ("POST", "/api/studio/projects/{pid}/publish/resolve"),
                      ("POST", "/api/studio/projects/{pid}/uploaded")):
            self.assertIn(route, paths)
        names = [c[0] for c in api_routes.bridge_commands()]
        self.assertIn("version", names)
        self.assertIn("top", names)


class FakeTreeTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.root)
        pkg = os.path.join(self.root, api_routes.PLATFORM)
        os.makedirs(os.path.join(pkg, "demo"))
        shutil.copy(os.path.join(PKG, "server.py"), pkg)
        self.service = os.path.join(pkg, "demo", "service.py")
        with open(self.service, "w", encoding="utf-8") as f:
            f.write(SERVICE)
        os.makedirs(os.path.join(self.root, "docs"))
        with open(os.path.join(self.root, api_routes.DOC), "w", encoding="utf-8") as f:
            f.write("# API\n\n<!-- gate:begin api-routes -->\n<!-- gate:end api-routes -->\n")

    def quiet(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            code = api_routes.sync(self.root, **kw)
        return code, err.getvalue()

    def test_parses_comment_docstring_body_status_and_alternatives(self):
        demo = [r for r in api_routes.collect(self.root) if r.module == "demo"]
        rows = {(r.method, r.path): r for r in demo}
        self.assertEqual(sorted(rows), [("GET", "/api/demo/a"), ("GET", "/api/demo/b"),
                                        ("GET", "/api/demo/items"), ("POST", "/api/demo/items/{item_id}")])
        self.assertEqual(rows[("GET", "/api/demo/items")].about, "Список всего")
        post = rows[("POST", "/api/demo/items/{item_id}")]
        self.assertEqual((post.about, post.body, post.response), ("Поздороваться", "JSON", "JSON 201"))

    def test_new_route_without_regeneration_fails_check(self):
        self.assertEqual(self.quiet()[0], 0)
        self.assertEqual(self.quiet(check=True)[0], 0)
        with open(self.service, "a", encoding="utf-8") as f:
            f.write('            case ("GET", ["new"]):\n                return req.send_json({})\n')
        code, err = self.quiet(check=True)
        self.assertEqual(code, 1)
        self.assertIn("python3 tools/api_routes.py", err)
        self.assertEqual(self.quiet()[0], 0)
        self.assertEqual(self.quiet(check=True)[0], 0)

    def test_missing_markers_fail(self):
        with open(os.path.join(self.root, api_routes.DOC), "w", encoding="utf-8") as f:
            f.write("# API\n")
        code, err = self.quiet(check=True)
        self.assertEqual(code, 1)
        self.assertIn("нет маркеров", err)


if __name__ == "__main__":
    unittest.main()
