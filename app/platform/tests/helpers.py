"""Общая обвязка тестов: платформа на фейковых бэкендах в отдельном потоке.

Базовые классы вкладок (StudioCase, PhotoServerCase, VideoServerCase, JournalCase) — здесь,
константы — в tests/fixtures.py: тест-модули друг друга не импортируют (QA-017).
"""

import http.client
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

from aiplatform import config as config_module
from aiplatform import server as server_module
from aiplatform.core import runner
from aiplatform.photo import config as photo_config
from aiplatform.photo.service import PhotoModule
from aiplatform.studio import steps
from aiplatform.video import config as video_config
from aiplatform.video.service import VideoModule
from tests import MISSING_MODELS_DIR, TMP_ROOT
from tests.fake_telegram import CHAT_METHODS, FakeTelegram, Rule
from tests.fixtures import ARTICLE, FAST, PNG_STUB, RENDER_SCALE, SOURCE, TOKEN, png

# Множитель всех ожиданий: под нагрузкой (параллельный прогон, рендер в фоне) — AIP_TEST_TIMEOUT_SCALE=3.
TIMEOUT_SCALE = float(os.environ.get("AIP_TEST_TIMEOUT_SCALE") or 1)


def scaled(seconds: float) -> float:
    return seconds * TIMEOUT_SCALE


def poll(fetch: Callable[[], Any], done: Callable[[Any], bool], timeout: float = 30, interval: float = 0.05, what=""):
    """Звать fetch(), пока done(результат) не станет истинным; возвращает последний результат.

    Единственное ожидание по стенным часам в тестах: timeout умножается на AIP_TEST_TIMEOUT_SCALE.
    Не дождались — AssertionError с последним результатом.
    """
    deadline = time.monotonic() + scaled(timeout)
    value = None
    while True:
        value = fetch()
        if done(value):
            return value
        if time.monotonic() >= deadline:
            raise AssertionError(f"не дождались {what}: {value}")
        time.sleep(interval)


def live_processes() -> list:
    """Живые подпроцессы раннера (Popen) — чтобы после отмены проверить, что их убили."""
    with runner._live_lock:
        return list(runner._live.values())


def isolated_config(env: dict[str, str]) -> dict[str, str]:
    """env для config.load без боевых путей.

    Нет AIP_DATA_DIR — свой временный каталог (иначе прочитается app/platform/data
    с чужими настройками). Не задан ни один каталог моделей (ключ *MODELS_DIR, хоть
    пустой) — несуществующий AIP_MODELS_DIR, чтобы не увидеть внешний диск с весами.
    Тест, проверяющий дефолт каталога моделей, передаёт его ключ пустым явно.
    """
    out = dict(env)
    if not out.get("AIP_DATA_DIR"):
        out["AIP_DATA_DIR"] = tempfile.mkdtemp(dir=TMP_ROOT)
    if not any(key.endswith("MODELS_DIR") for key in out):
        out["AIP_MODELS_DIR"] = MISSING_MODELS_DIR
    return out


class PlatformCase(unittest.TestCase):
    """Сервер на порту 0 с фейковыми бэкендами; modules — какие вкладки поднять.

    Рендер студии по умолчанию в четверть размера (AIP_RENDER_SCALE): extra_env может переопределить.
    """

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
        base = {"AIP_BACKEND": "fake", "AIP_DATA_DIR": self.tmp.name, "AIP_PORT": "0", "AIP_RENDER_SCALE": RENDER_SCALE}
        env = isolated_config(base | self.extra_env)
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
        # Как ui.js api(): служебный X-AIP; None в headers — заголовок не слать.
        headers = {k: v for k, v in {"X-AIP": "1", **(headers or {})}.items() if v is not None}
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=scaled(20)) as resp:
                return resp.status, resp.headers, resp.read()
        except urllib.error.HTTPError as err:
            return err.code, err.headers, err.read()

    def json(self, method, path, body=None, headers=None):
        status, _, raw = self.call(method, path, body, headers)
        return status, json.loads(raw)

    def raw(self, method, path):
        """Без перехода по редиректам: (статус, заголовки)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=scaled(10))
        try:
            conn.request(method, path)
            resp = conn.getresponse()
            resp.read()
            return resp.status, resp.headers
        finally:
            conn.close()

    def poll(self, path, done, timeout=30, interval=0.05):
        """GET path, пока done(ответ) не станет истинным; возвращает последний ответ."""
        try:
            return poll(lambda: self.json("GET", path)[1], done, timeout, interval, what=path)
        except AssertionError as exc:
            self.fail(str(exc))

    def wait_job(self, path, statuses=("done", "failed", "canceled"), timeout=20):
        """Задача вкладки (фото, видео) до одного из statuses: (последний ответ, все ответы по пути)."""
        seen = []

        def fetch():
            seen.append(self.json("GET", path)[1])
            return seen[-1]

        try:
            job = poll(fetch, lambda j: j["status"] in statuses, timeout, 0.02, what=f"{path} → {statuses}")
        except AssertionError as exc:
            self.fail(str(exc))
        return job, seen


class StudioCase(PlatformCase):
    """Студия на быстрых фейках: проекты, ожидание конвейера, каталог проекта."""

    process_env = FAST

    def create(self, **payload):
        if payload.get("scenario") == "article":  # без источника статья не публикуется (CMP-005)
            payload["settings"] = SOURCE | (payload.get("settings") or {})
        status, project = self.json("POST", "/api/studio/projects", payload)
        self.assertEqual(status, 201, project)
        return project

    def settle(self, pid, timeout=90):
        return self.poll(f"/api/studio/projects/{pid}", lambda p: not p["running"], timeout=timeout, interval=0.1)

    @staticmethod
    def statuses(project):
        return {s["id"]: s["status"] for s in project["steps"]}

    def folder(self, pid) -> Path:
        return Path(self.tmp.name) / "studio" / "projects" / pid


# --- вкладки «Фото» и «Видео» отдельно от остальных ---


def photo_only(ctx):
    return [PhotoModule(ctx)]


def photo_load(env):
    return photo_config.load(config_module.load(isolated_config(env)))


class PhotoServerCase(PlatformCase):
    step_delay = "0.01"
    fail_flag = ""
    modules = staticmethod(photo_only)

    def setUp(self):
        self.process_env = {"FAKE_GEN_STEP_DELAY": self.step_delay, "FAKE_GEN_FAIL": self.fail_flag}
        super().setUp()

    def upload(self, width=512, height=384):
        status, body = self.json("POST", "/api/photo/uploads", png(width, height, 1))
        self.assertEqual(status, 201, body)
        self.assertEqual((body["width"], body["height"]), (width, height))
        return body["id"]

    def wait(self, job_id, statuses=("done", "failed", "canceled"), timeout=20):
        return self.wait_job(f"/api/photo/jobs/{job_id}", statuses, timeout)

    def run_job(self, payload):
        status, job = self.json("POST", "/api/photo/jobs", payload)
        self.assertEqual(status, 201, job)
        done, seen = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        _, _, image = self.call("GET", done["image_url"])
        return done, seen, image


def video_only(ctx):
    return [VideoModule(ctx)]


def video_load(env):
    return video_config.load(config_module.load(isolated_config(env)))


class VideoServerCase(PlatformCase):
    step_delay = "0.01"
    fail_flag = ""
    modules = staticmethod(video_only)
    extra_env = {"AIP_MODELS_DIR": "/nonexistent-models"}  # не зависеть от весов на этой машине

    def setUp(self):
        self.process_env = {
            "FAKE_LTX_STEP_DELAY": self.step_delay,
            "FAKE_LTX_NO_FFMPEG": "1",
            "FAKE_LTX_FAIL": self.fail_flag,
        }
        super().setUp()

    def upload(self):
        status, body = self.json("POST", "/api/video/uploads", PNG_STUB)
        self.assertEqual(status, 201, body)
        return body["id"]

    def wait(self, job_id, statuses=("done", "failed", "canceled"), timeout=20):
        return self.wait_job(f"/api/video/jobs/{job_id}", statuses, timeout)


# --- публикация в два Telegram-канала с журналом (BE-001) ---

CHATS = {"tg-a": "-1001111111111", "tg-b": "-1002222222222"}


def chat_of(body: bytes) -> str:
    for cid, chat in CHATS.items():
        if f'name="chat_id"\r\n\r\n{chat}\r\n'.encode() in body:
            return cid
    return "?"


def chat_of_id(chat_id: str) -> str:
    """Канал CHATS по chat_id запроса; чужой — «?»."""
    return next((cid for cid, chat in CHATS.items() if chat == chat_id), "?")


class JournalTelegram(FakeTelegram):
    """Фейковый Bot API на HTTP (tests/fake_telegram.py) с прежним интерфейсом журнала BE-001.

    refuse — (канал, метод), на которые Telegram отвечает отказом ok=false; drop — обрыв без
    ответа после отправки. calls — (канал, метод, принят ли пост) без методов о канале.
    """

    def __init__(self):
        super().__init__()
        self.refuse: set[tuple[str, str]] = set()
        self.drop: set[tuple[str, str]] = set()

    def _rule(self, call):
        key = (chat_of_id(call.chat), call.method)
        if key in self.drop:
            return Rule(call.method, "drop", 0)
        if key in self.refuse:
            return Rule(call.method, "refuse", 0, description="Bad Request: not enough rights to send")
        return super()._rule(call)

    @property
    def calls(self) -> list[tuple[str, str, bool]]:
        return [(chat_of_id(c.chat), c.method, c.accepted) for c in self.sent() if c.method not in CHAT_METHODS]

    def count(self, channel, method, *, accepted=None):
        return sum(
            1 for c, m, ok in self.calls if c == channel and m == method and (accepted is None or ok == accepted)
        )


FakeTelegram = JournalTelegram  # noqa: F811 — прежнее имя: на него ссылаются ветки


class JournalCase(StudioCase):
    """Два Telegram-канала (CHATS) с токеном из AIP_TG_TEST; self.tg — фейковый Bot API."""

    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        self.channels_file = folder / "channels.json"
        self.channels_file.write_text(
            json.dumps(
                [
                    {"id": cid, "label": cid.upper(), "type": "telegram", "chat_id": chat, "token_env": "AIP_TG_TEST"}
                    for cid, chat in CHATS.items()
                ]
            ),
            encoding="utf-8",
        )
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(self.channels_file)}
        super().setUp()
        # Настоящий HTTP: multipart, коды ответов и обрывы идут через publish._http_post (QA-007).
        self.tg = JournalTelegram().start()
        self.addCleanup(self.tg.stop)
        patcher = mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN, **self.tg.env()})
        patcher.start()
        self.addCleanup(patcher.stop)

    def video_project(self) -> str:
        pid = self.create(scenario="article", text=ARTICLE)["id"]
        (self.folder(pid) / "video.mp4").write_bytes(b"x")
        return pid

    def press(self, pid, **body):
        status, view = self.json("POST", f"/api/studio/projects/{pid}/publish", body)
        self.assertEqual(status, 202, view)
        return self.settle(pid)

    def resolve(self, pid, channel, posted):
        status, view = self.json(
            "POST", f"/api/studio/projects/{pid}/publish/resolve", {"channel": channel, "posted": posted}
        )
        self.assertEqual(status, 200, view)
        return self.settle(pid)

    def journal(self, pid) -> dict:
        data = steps.load_published(self.folder(pid))
        return {r["channel"]: r for r in data}
