"""ui.js h(): чужие ссылки (javascript:, data:text/html) не становятся атрибутами (SEC-003).

Гоняется в node с заглушкой DOM; нет node — тест пропускается.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

UI_JS = Path(__file__).resolve().parent.parent / "aiplatform" / "static" / "ui.js"

SCRIPT = """
globalThis.location = { href: "http://127.0.0.1:8780/news/" };
class N { constructor() { this.a = {}; } setAttribute(k, v) { this.a[k] = v; } addEventListener() {} append() {} }
globalThis.document = { createElement: () => new N(), createTextNode: (s) => s };
globalThis.Node = N;
const { h } = await import(process.argv[1]);
const cases = JSON.parse(process.argv[2]);
console.log(JSON.stringify(cases.map(([k, v]) => k in h("a", { [k]: v }).a)));
"""

CASES = [
    ("href", "javascript:alert(1)", False),
    ("href", " JaVaScRiPt:alert(1)", False),
    ("href", "java\tscript:alert(1)", False),
    ("href", "vbscript:x", False),
    ("href", "data:text/html,<script>alert(1)</script>", False),
    ("href", "data:image/png;base64,AA", False),
    ("action", "javascript:x", False),
    ("formaction", "javascript:x", False),
    ("href", "https://go.dev/blog", True),
    ("href", "http://x.test/a", True),
    ("href", "/studio/?scenario=digest", True),
    ("href", "?scenario=article&project=1", True),
    ("href", "blob:http://127.0.0.1:8780/abc", True),
    ("src", "data:image/png;base64,AA", True),
    ("src", "/api/photo/jobs/1/image", True),
    ("poster", "javascript:x", False),
]


@unittest.skipUnless(shutil.which("node"), "нет node")
class SafeUrlTest(unittest.TestCase):
    def test_h_drops_dangerous_urls(self):
        payload = json.dumps([[k, v] for k, v, _ in CASES])
        out = subprocess.run(
            ["node", "--input-type=module", "-e", SCRIPT, UI_JS.as_uri(), payload],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        got = json.loads(out.stdout)
        for (key, value, expected), kept in zip(CASES, got, strict=True):
            with self.subTest(key=key, value=value):
                self.assertEqual(kept, expected)


if __name__ == "__main__":
    unittest.main()
