"""Вызов движка narrator: мост `narrate bridge` и команды на фейковом движке."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from aiplatform import config as platform_config
from aiplatform.core import narrator
from tests.helpers import PlatformCase


class NarratorTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        config = platform_config.load({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp.name})
        self.narrator = narrator.load(config)

    def test_real_defaults_point_at_app_narrator(self):
        config = platform_config.load({"AIP_DATA_DIR": "/tmp/x"})
        real = narrator.load(config)
        self.assertEqual(real.home.name, "narrator")
        self.assertEqual(real.home.parent.name, "app")
        self.assertEqual(real.cmd, (str(real.home / "narrate"),))
        self.assertEqual(
            real.make("news-rescue", DAY="2026-10-01", X=""),
            ["make", "-C", str(real.home), "news-rescue", "DAY=2026-10-01"],
        )
        # Пометки в подписях narrator — по реестру платформы (ADR 0001).
        self.assertEqual(real.env, {narrator.REGISTRY_ENV: str(config.data_dir / "registry.json")})
        own = narrator.load(platform_config.load({"AIP_DATA_DIR": "/tmp/x", "NARRATOR_REGISTRY": "/r.json"}))
        self.assertEqual(own.env[narrator.REGISTRY_ENV], "/r.json")

    def test_bridge_engines_and_preview(self):
        engines = self.narrator.bridge("engines")
        by_id = {e["id"]: e for e in engines["engines"]}
        self.assertIn("модель не скачана", by_id["fish"]["problem"])
        self.assertIsNone(by_id["higgs"]["problem"])
        preview = self.narrator.bridge("preview", stdin="Go 26\n")
        self.assertEqual(preview["script"], "го два шесть\n")
        self.assertEqual(preview["changed"], 1)

    def test_bridge_error_and_state(self):
        with self.assertRaisesRegex(narrator.NarratorError, "Нужны название и текст"):
            self.narrator.bridge("article-new", stdin={"title": "x"})
        made = self.narrator.bridge("article-new", stdin={"title": "Проба", "text": "Текст."})
        library = self.narrator.bridge("library")
        self.assertIn(made["id"], [a["id"] for a in library["articles"]])
        self.assertTrue(Path(self.narrator.env[narrator.FAKE_HOME_ENV], "state.json").is_file())
        with self.assertRaisesRegex(narrator.NarratorError, "нет в библиотеке"):
            self.narrator.bridge("article", "nope")

    def test_missing_command(self):
        broken = narrator.Narrator(("/нет/такой/narrate",), Path("/нет"))
        self.assertFalse(broken.available())
        with self.assertRaisesRegex(narrator.NarratorError, "не найдена"):
            broken.bridge("engines")
        narrator.reset_checks()
        self.assertIn("не найдена", broken.check())


def _script(folder: Path, body: str) -> Path:
    path = folder / "narrate"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)
    return path


class CheckTest(unittest.TestCase):
    """check(): старый движок без bridge, без bridge version, чужая версия моста."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        narrator.reset_checks()
        self.addCleanup(narrator.reset_checks)

    def engine(self, body: str) -> narrator.Narrator:
        return narrator.Narrator((str(_script(self.dir, body)),), self.dir)

    def test_old_engine_without_bridge(self):
        old = self.engine(
            'echo "usage: narrate [-h] {say,synth} ..." >&2\n'
            "echo \"narrate: error: argument cmd: invalid choice: 'bridge' (choose from 'say', 'synth')\" >&2\n"
            "exit 2\n"
        )
        problem = old.check()
        self.assertIn("нет команды bridge", problem)
        self.assertIn(str(self.dir), problem)
        self.assertIn("AIP_NARRATOR_DIR", problem)

    def test_bridge_without_version(self):
        old = self.engine(
            "echo \"narrate bridge: error: invalid choice: 'version' (choose from 'engines')\" >&2\nexit 2\n"
        )
        self.assertIn("нет bridge version", old.check())

    def test_version_mismatch_and_ok(self):
        self.assertIn("версия моста 99, нужна", self.engine("echo '{\"bridge_api\": 99}'\n").check())
        narrator.reset_checks()
        ok = f"echo '{{\"bridge_api\": {narrator.EXPECTED_BRIDGE_API}}}'\n"
        self.assertIsNone(self.engine(ok).check())

    def test_result_is_cached(self):
        calls = self.dir / "calls"
        engine = self.engine(f'echo x >> "{calls}"\necho \'{{"bridge_api": {narrator.EXPECTED_BRIDGE_API}}}\'\n')
        self.assertIsNone(engine.check())
        self.assertIsNone(narrator.Narrator(engine.cmd, engine.home).check())  # другой экземпляр — тот же кеш
        self.assertEqual(calls.read_text().count("x"), 1)

    def test_fake_engine_is_compatible(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = platform_config.load({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp})
            self.assertIsNone(narrator.load(config).check())


class ErrorKindTest(unittest.TestCase):
    """NarratorError.kind: из поля kind моста или по способу сбоя; маппинг в HTTP-код (BE-004)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        config = platform_config.load({"AIP_BACKEND": "fake", "AIP_DATA_DIR": tmp.name})
        self.fake = narrator.load(config)

    def kind(self, engine: narrator.Narrator, *args: str, **kw) -> str:
        with self.assertRaises(narrator.NarratorError) as ctx:
            engine.bridge(*args, **kw)
        return ctx.exception.kind

    def engine(self, body: str) -> narrator.Narrator:
        return narrator.Narrator((str(_script(self.dir, body)),), self.dir)

    def test_kind_from_bridge(self):
        self.assertEqual(self.kind(self.fake, "article", "nope"), narrator.NOT_FOUND)
        self.assertEqual(self.kind(self.fake, "article-new", stdin={"title": "x"}), narrator.INVALID)

    def test_engine_failures_are_unavailable(self):
        missing = narrator.Narrator(("/нет/такой/narrate",), Path("/нет"))
        self.assertEqual(self.kind(missing, "library"), narrator.UNAVAILABLE)
        self.assertEqual(self.kind(self.engine("echo boom >&2\nexit 1\n"), "library"), narrator.UNAVAILABLE)
        self.assertEqual(self.kind(self.engine("sleep 5\n"), "library", timeout=0.2), narrator.UNAVAILABLE)

    def test_old_engine_error_without_kind_is_invalid(self):
        engine = self.engine('echo \'{"error": "что-то не так"}\'\nexit 1\n')
        self.assertEqual(self.kind(engine, "library"), narrator.INVALID)
        engine = self.engine('echo \'{"error": "x", "kind": "странный"}\'\nexit 1\n')
        self.assertEqual(self.kind(engine, "library"), narrator.INVALID)

    def test_http_mapping(self):
        for kind, status in ((narrator.UNAVAILABLE, 503), (narrator.NOT_FOUND, 404), (narrator.INVALID, 400)):
            err = narrator.http_error(narrator.NarratorError("текст", kind))
            self.assertEqual((err.status, err.message, err.extra), (status, "текст", {"kind": kind}))


class BridgeRetryTest(unittest.TestCase):
    """Чтение моста, упавшее на запуске, повторяется один раз; таймаут и запись — нет (BE-013)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.calls = self.dir / "calls"
        self.sleeps: list[float] = []
        patch = mock.patch.object(narrator, "_sleep", self.sleeps.append)
        patch.start()
        self.addCleanup(patch.stop)

    def engine(self, body: str) -> narrator.Narrator:
        return narrator.Narrator((str(_script(self.dir, f'echo x >> "{self.calls}"\n' + body)),), self.dir)

    def count(self) -> int:
        return self.calls.read_text().count("x") if self.calls.exists() else 0

    def test_read_crash_then_ok(self):
        # Первый запуск падает, второй отвечает: файл-метка отличает заходы.
        mark = self.dir / "mark"
        engine = self.engine(
            f'if [ ! -f "{mark}" ]; then touch "{mark}"; echo boom >&2; exit 1; fi\necho \'{{"items": []}}\'\n'
        )
        self.assertEqual(engine.bridge("library"), {"items": []})
        self.assertEqual(self.count(), 2)
        self.assertEqual(self.sleeps, [narrator.RETRY_PAUSE_S])

    def test_read_crash_twice_raises_unavailable(self):
        with self.assertRaises(narrator.NarratorError) as ctx:
            self.engine("echo boom >&2\nexit 1\n").bridge("library")
        self.assertEqual(ctx.exception.kind, narrator.UNAVAILABLE)
        self.assertEqual(self.count(), 2)

    def test_timeout_is_not_retried(self):
        engine = self.engine("sleep 8\n")
        started = time.monotonic()
        with self.assertRaises(narrator.NarratorError):
            engine.bridge("library", timeout=1.5)
        self.assertLess(time.monotonic() - started, 3.5)
        self.assertEqual(self.count(), 1)
        self.assertEqual(self.sleeps, [])

    def test_write_and_refusal_are_not_retried(self):
        with self.assertRaises(narrator.NarratorError):
            self.engine("echo boom >&2\nexit 1\n").bridge("article-save", "a1", stdin="{}")
        self.assertEqual(self.count(), 1)
        with self.assertRaises(narrator.NarratorError):
            self.engine('echo \'{"error": "нет", "kind": "not_found"}\'\nexit 1\n').bridge("article", "x")
        self.assertEqual(self.count(), 2)


class UnavailableEngineHttpTest(PlatformCase):
    """Недоступный движок даёт 503 на всех ручках, которые ходят в мост."""

    def setUp(self):
        super().setUp()
        broken = narrator.Narrator(("/нет/такой/narrate",), Path("/нет"))
        for prefix in ("library", "news", "channels", "audio", "updates"):
            self.app.modules[prefix].narrator = broken

    def test_503_everywhere(self):
        for path in (
            "/api/library/state",
            "/api/news/sources",
            "/api/channels/subscribers",
            "/api/audio/library",
            "/api/updates/voice-models",
        ):
            status, body = self.json("GET", path)
            self.assertEqual(status, 503, (path, body))
            self.assertEqual(body["kind"], "unavailable")
            self.assertIn("не найдена", body["error"])

    def test_missing_article_is_404(self):
        self.app.modules["library"].narrator = narrator.load(self.config)
        status, body = self.json("GET", "/api/library/articles/nope")
        self.assertEqual((status, body["kind"]), (404, "not_found"))


if __name__ == "__main__":
    unittest.main()
