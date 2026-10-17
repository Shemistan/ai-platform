"""Проверка свободного места до GPU-задач и сборки (REL-013): порог, ошибка, уведомление, замок."""

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiplatform.core import disk
from aiplatform.core import settings as settings_module
from aiplatform.core.gate import Gate
from aiplatform.studio import steps
from aiplatform.studio.llm import StudioError
from tests.helpers import PlatformCase

TOO_MUCH_GB = 9000  # заведомо больше свободного на любом диске; в пределах MAX_FREE_GB


class EnsureFreeSpaceTest(unittest.TestCase):
    def test_enough_returns_free(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertGreater(disk.ensure_free_space(Path(tmp), 0.001), 0)
            # Каталога ещё нет — смотрим ближайший существующий родитель.
            self.assertGreater(disk.free_gb(Path(tmp) / "нет" / "такого"), 0)

    def test_not_enough_is_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp, self.assertRaises(disk.NoSpace) as caught:
            disk.ensure_free_space(Path(tmp), TOO_MUCH_GB, "Фото")
        text = str(caught.exception)
        self.assertRegex(text, r"^Фото: свободно \d+\.\d ГБ на диске данных, нужно от 9000 ГБ\.")
        self.assertIn("min_free_gb", text)

    def test_threshold_defaults(self):
        empty = settings_module.normalize({})
        self.assertIsNone(empty["cleanup"]["min_free_gb"])
        self.assertEqual(disk.threshold(empty, fake=False), 10.0)
        self.assertEqual(disk.threshold(empty, fake=True), 0.0)  # демо и тесты — без проверки
        custom = settings_module.normalize({"cleanup": {"min_free_gb": 25}})
        self.assertEqual(disk.threshold(custom, fake=True), 25.0)
        self.assertIsNone(settings_module.normalize({"cleanup": {"min_free_gb": -1}})["cleanup"]["min_free_gb"])


class GuardTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.sent = []
        self.now = 1000.0

    def guard(self, need):
        return disk.Guard(self.path, lambda: need, lambda t, x: self.sent.append((t, x)), clock=lambda: self.now)

    def test_off_and_enough_pass_silently(self):
        self.guard(0)("Фото")
        self.guard(0.001)("Фото")
        self.assertEqual(self.sent, [])

    def test_low_space_raises_and_notifies_once_an_hour(self):
        guard = self.guard(TOO_MUCH_GB)
        for _ in range(3):
            with self.assertRaises(disk.NoSpace):
                guard("Видео")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0][0], "Мало места на диске")
        self.assertIn("Видео: свободно", self.sent[0][1])
        self.now += disk.NOTIFY_EVERY_S + 1
        with self.assertRaises(disk.NoSpace):
            guard("Видео")
        self.assertEqual(len(self.sent), 2)


class GateGuardTest(unittest.TestCase):
    def test_guard_failure_releases_lock(self):
        def guard(what):
            raise disk.NoSpace(f"{what}: мало места")

        gate = Gate(guard=guard)
        with self.assertRaises(disk.NoSpace), gate.hold("Фото"):
            self.fail("задача не должна стартовать")
        self.assertIsNone(gate.holder())
        gate.guard = None
        done = threading.Event()
        with gate.hold("Фото"):
            done.set()
        self.assertTrue(done.is_set())


class RenderSpaceTest(unittest.TestCase):
    def test_render_step_fails_with_clear_error(self):
        def space(what):
            raise disk.NoSpace(f"{what}: свободно 1.0 ГБ на диске данных, нужно от 10 ГБ.")

        ctx = SimpleNamespace(studio=SimpleNamespace(services=SimpleNamespace(space=space)))
        with self.assertRaisesRegex(StudioError, "^Сборка видео: свободно 1.0 ГБ"):
            steps.ensure_space(ctx, "Сборка видео")
        ctx.studio.services.space = None
        steps.ensure_space(ctx, "Сборка видео")  # без проверки — без ошибки


class GpuTaskTest(PlatformCase):
    def test_photo_job_does_not_start_without_space(self):
        # Порог выше любого диска и включённые уведомления (без каналов — только журнал).
        (Path(self.tmp.name) / "settings.json").write_text(
            json.dumps({"cleanup": {"min_free_gb": TOO_MUCH_GB}}), encoding="utf-8"
        )
        status, job = self.json("POST", "/api/photo/jobs", {"prompt": "кот", "model": "z-image-turbo"})
        self.assertEqual(status, 201, job)
        deadline = time.time() + 20
        while time.time() < deadline:
            _, job = self.json("GET", f"/api/photo/jobs/{job['id']}")
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.05)
        self.assertEqual(job["status"], "failed", job)
        self.assertIn("свободно", job["error"])
        self.assertIn("нужно от 9000 ГБ", job["error"])
        self.assertIsNone(self.app.ctx.gate.holder(), "замок отпущен")
        log = (Path(self.tmp.name) / "notifications.log").read_text(encoding="utf-8")
        self.assertIn("Мало места на диске", log)


if __name__ == "__main__":
    unittest.main()
