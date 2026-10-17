"""HTTP-сервер платформы: вкладки-модули под /api/<prefix>/, статика, общая шапка.

Запуск: `python3 -m aiplatform.server` (или `make run` / `make demo`).
"""

from __future__ import annotations

import logging
import mimetypes
import os
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from aiplatform import __version__
from aiplatform import config as config_module
from aiplatform.config import Config
from aiplatform.core import disk, log, media, runner
from aiplatform.core.gate import Gate
from aiplatform.core.http import HttpError, Request, not_found
from aiplatform.core.notify import Notifier
from aiplatform.core.uploads import UploadStore

STATIC_DIR = Path(__file__).with_name("static")
HOME = "/photo/"
MIN_FREE_GB = 5.0  # меньше свободного на диске данных — /api/ready не готов (AIP_MIN_FREE_GB)
ALLOW = "GET, HEAD, POST"


class Module(Protocol):
    """Вкладка платформы: свой префикс API и своя страница static/<prefix>/."""

    prefix: str
    label: str

    def handle(self, req: Request) -> None: ...  # не свой путь → HttpError 404

    def describe(self) -> dict: ...

    def problems(self) -> list[str]: ...

    def close(self) -> None: ...


@dataclass
class Context:
    """Общее для модулей: настройки, GPU-замок, загрузки, уведомления, соседние модули."""

    config: Config
    gate: Gate
    uploads: UploadStore
    notifier: Notifier
    modules: dict[str, Module] = field(default_factory=dict)


ModulesFactory = Callable[[Context], list[Module]]


def default_modules(ctx: Context) -> list[Module]:
    """Все вкладки платформы в порядке шапки; сценарии — после вкладок, которыми пользуются."""
    from aiplatform.audio.service import AudioModule
    from aiplatform.channels.service import ChannelsModule
    from aiplatform.library.service import LibraryModule
    from aiplatform.news.service import NewsModule
    from aiplatform.photo.service import PhotoModule
    from aiplatform.settings.service import SettingsModule
    from aiplatform.studio.service import StudioModule
    from aiplatform.updates.service import UpdatesModule
    from aiplatform.video.service import VideoModule

    return [
        PhotoModule(ctx),
        VideoModule(ctx),
        AudioModule(ctx),
        StudioModule(ctx),
        LibraryModule(ctx),
        NewsModule(ctx),
        ChannelsModule(ctx),
        UpdatesModule(ctx),
        SettingsModule(ctx),
    ]


class App:
    def __init__(self, config: Config, modules: ModulesFactory = default_modules) -> None:
        self.config = config
        self.started = time.monotonic()
        self.stopping = False  # пришёл SIGTERM: новые задачи не принимаются (503)
        self.closed = False
        # Процессы моделей прошлого запуска, пережившие его (SIGKILL, сбой), держат память — гасим.
        self.run_dir = config.data_dir / "run"
        self.orphans = runner.reap_orphans(self.run_dir)
        runner.configure(self.run_dir, lambda: config.settings.load()["timeouts"])
        notifier = Notifier(config.data_dir, config_module.pick(config.env, "AIP_NOTIFY_CMD"), fake=config.fake)
        gate = Gate(guard=disk.guard_for(config, notifier))  # мало места — GPU-задача не стартует
        self.ctx = Context(config=config, gate=gate, uploads=UploadStore(config.uploads_dir), notifier=notifier)
        for module in modules(self.ctx):
            self.ctx.modules[module.prefix] = module
        for module in self.ctx.modules.values():  # модулю нужны соседи — они уже созданы
            bind = getattr(module, "bind", None)
            if bind:
                bind()

    @property
    def modules(self) -> dict[str, Module]:
        return self.ctx.modules

    def models_dir(self) -> Path | None:
        """Каталог весов фото (AIP_MODELS_DIR, по умолчанию внешний SSD); в демо модели не нужны."""
        photo = self.modules.get("photo")
        if self.config.fake or photo is None:
            return None
        return Path(photo.config.models_dir)

    def problems(self) -> list[str]:
        out = []
        if not media.ffmpeg_available(self.config.ffmpeg):
            out.append("ffmpeg не найден: сборка видео и демо-генерация не работают — brew install ffmpeg.")
        if not self.config.font:
            out.append("Не найден шрифт с кириллицей для слайдов — задай AIP_FONT=/путь/к/шрифту.ttf.")
        models = self.models_dir()
        if models is not None and not models.is_dir():
            out.append(
                f"Каталог моделей {models} не найден — внешний диск не примонтирован? "
                "Подключи его или задай AIP_MODELS_DIR."
            )
        for module in self.modules.values():
            out.extend(module.problems())
        return list(dict.fromkeys(out))  # общий движок (narrator) жалуется из нескольких вкладок

    def describe(self) -> dict:
        return {
            "backend": self.config.backend,
            "tabs": [{"id": m.prefix, "label": m.label} for m in self.modules.values()],
            "problems": self.problems(),
        }

    def health(self) -> dict:
        """Живость для launchd и внешнего чекера: без ffmpeg, narrator и сети — только память и диск."""
        out = {"ok": True, "version": __version__, "uptime_s": int(time.monotonic() - self.started)}
        scheduler = getattr(self.modules.get("studio"), "scheduler", None)
        if scheduler is not None:  # чекеру: включено ли расписание и чем кончился последний запуск
            plan = self.config.settings.load()["schedule"]["digest"]
            out["schedule"] = {**plan, "last": scheduler.digest_run()}
        return out

    def ready(self) -> dict:
        """Готов ли сервис выпустить дайджест: проверки по именам; ok=False → /api/ready отдаёт 503."""
        checks: list[dict] = []

        def check(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"name": name, "ok": ok, "detail": detail})

        problems = self.problems()
        check("problems", not problems, "; ".join(problems))
        try:
            free_gb = shutil.disk_usage(self.config.data_dir).free / 2**30
        except OSError as exc:
            check("disk", False, f"{self.config.data_dir}: {exc}")
        else:
            need = float(config_module.pick(self.config.env, "AIP_MIN_FREE_GB", default=str(MIN_FREE_GB)))
            check("disk", free_gb >= need, f"свободно {free_gb:.1f} ГБ, нужно от {need:g} ГБ")
        models = self.models_dir()
        if models is not None:
            check("models_dir", models.is_dir(), str(models))
        for module in self.modules.values():  # расписание, токен каналов дайджеста, последний дайджест
            extra = getattr(module, "ready_checks", None)
            if extra:
                checks.extend(extra())
        if self.stopping:
            check("stopping", False, "сервис останавливается")
        failed = [c["name"] for c in checks if not c["ok"]]
        return {"ok": not failed, "failed": failed, "checks": checks, "problems": problems}

    def status(self) -> dict:
        out = {"gpu": self.ctx.gate.holder(), "waiting": self.ctx.gate.waiting()}
        held = self.ctx.gate.held()
        if held:  # видно, если модель зависла: сколько секунд замок не отпускают
            out["gpu_since"] = held["since"]
            out["gpu_held_s"] = held["held_s"]
        for module in self.modules.values():  # счётчики шапки: например, сколько выпусков ждут человека
            badges = getattr(module, "badges", None)
            if badges:
                out |= badges()
        return out

    def close(self) -> None:
        if self.closed:  # SIGTERM закрывает в фоне, main — ещё раз в finally
            return
        self.closed = True
        self.stopping = True
        # Сначала гасим все процессы моделей разом, потом ждём потоки модулей — иначе каждый
        # модуль ждал бы свой процесс по очереди.
        killed = runner.kill_all()
        if killed:
            log.event("process.killed_on_stop", count=killed)
        for module in reversed(list(self.modules.values())):
            module.close()
        runner.configure(None)  # pid-файлы и флаг остановки — только этого App (тесты поднимают их подряд)


class Handler(BaseHTTPRequestHandler):
    server_version = "aiplatform"
    protocol_version = "HTTP/1.1"
    app: App  # задаётся в make_server

    def log_message(self, fmt: str, *args: object) -> None:  # stderr не шумит: доступ пишет log_request
        pass

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        """Access-лог: действия (не GET) и ошибки; опросы (/api/status, задачи, health) — нет."""
        method = getattr(self, "command", "") or ""
        status = int(code) if str(code).isdigit() else 0
        if method in ("GET", "HEAD") and status < 400:
            return
        log.event("http.access", method=method, path=urlsplit(self.path).path, status=status)

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET", head=True)

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _not_allowed(self) -> None:  # без них BaseHTTPRequestHandler отвечает HTML-страницей 501
        self._dispatch(self.command)

    do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    def _dispatch(self, method: str, *, head: bool = False) -> None:
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        req = Request(self, method, parts, parse_qs(url.query), head=head)
        try:
            if method not in ("GET", "POST"):
                raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "Метод не поддерживается.", headers={"Allow": ALLOW})
            # Чужая страница в браузере не должна запускать генерацию на localhost.
            origin = self.headers.get("Origin")
            if method == "POST" and origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise HttpError(HTTPStatus.FORBIDDEN, "Запрос с чужого сайта отклонён.")
            if method == "POST" and self.app.stopping:
                raise HttpError(HTTPStatus.SERVICE_UNAVAILABLE, "Сервис останавливается — повтори после перезапуска.")
            if parts[:1] != ["api"]:
                if method != "GET":
                    raise HttpError(
                        HTTPStatus.METHOD_NOT_ALLOWED, "Метод не поддерживается.", headers={"Allow": "GET, HEAD"}
                    )
                return self._static(req, url.path, parts)
            match (method, parts[1:]):
                case ("GET", ["config"]):
                    return req.send_json(self.app.describe())
                case ("GET", ["status"]):
                    return req.send_json(self.app.status())
                case ("GET", ["health"]):
                    return req.send_json(self.app.health())
                case ("GET", ["ready"]):
                    ready = self.app.ready()
                    return req.send_json(ready, HTTPStatus.OK if ready["ok"] else HTTPStatus.SERVICE_UNAVAILABLE)
                case (_, [prefix, *rest]) if prefix in self.app.modules:
                    req.sub = rest
                    return self.app.modules[prefix].handle(req)
            raise not_found()
        except HttpError as exc:
            req.send_json({"error": exc.message, **exc.extra}, exc.status, exc.headers)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception:  # баг в модуле: ответить JSON, а не рвать соединение молча
            self._internal_error(req)

    def _internal_error(self, req: Request) -> None:
        request_id = uuid.uuid4().hex[:12]
        # В лог процесса с трассировкой; request_id — тот же, что ушёл клиенту.
        log.event(
            "http.error",
            level=logging.ERROR,
            exc=True,
            method=self.command,
            path=urlsplit(self.path).path,
            request_id=request_id,
        )
        if req.started:  # часть ответа уже ушла — второй не склеить
            self.close_connection = True
            return
        body = {"error": "Внутренняя ошибка", "code": "internal", "request_id": request_id}
        self.close_connection = True  # тело запроса могло остаться непрочитанным
        try:
            req.send_json(body, HTTPStatus.INTERNAL_SERVER_ERROR)
        except OSError:
            self.close_connection = True

    def _static(self, req: Request, path: str, parts: list[str]) -> None:
        if not parts:
            return req.redirect(HOME)
        # /photo → /photo/: относительные пути страницы считаются от каталога вкладки.
        if len(parts) == 1 and (STATIC_DIR / parts[0]).is_dir() and not path.endswith("/"):
            return req.redirect(f"/{parts[0]}/")
        name = path.lstrip("/")
        if name.endswith("/") or (STATIC_DIR / name).is_dir():
            name = name.rstrip("/") + "/index.html"
        target = (STATIC_DIR / name).resolve()
        if STATIC_DIR.resolve() not in target.parents or not target.is_file():
            raise not_found()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        req.send_file(target, ctype, cache="no-cache")


def make_server(config: Config, modules: ModulesFactory = default_modules) -> tuple[ThreadingHTTPServer, App]:
    app = App(config, modules)
    handler = type("BoundHandler", (Handler,), {"app": app})
    server = ThreadingHTTPServer((config.host, config.port), handler)
    server.daemon_threads = True
    return server, app


def request_stop(server: ThreadingHTTPServer, app: App, reason: str) -> None:
    """SIGTERM (launchd, kill, перезагрузка): новые POST — 503, задачи и процессы моделей
    гасятся, затем сервер выходит из serve_forever.

    Всё — в отдельном потоке, не в обработчике сигнала: shutdown() ждёт выхода из цикла,
    а пока идёт app.close(), сервер ещё отвечает (чтение — как обычно, POST — 503)."""
    if app.stopping:
        return
    app.stopping = True
    log.event("process.stopping", reason=reason)

    def stop() -> None:
        try:
            app.close()
        finally:
            server.shutdown()

    threading.Thread(target=stop, name="shutdown", daemon=True).start()


def main() -> None:
    config = config_module.load()
    log_path = log.setup(config.data_dir, stderr=config_module.flag(config.env, "AIP_LOG_STDERR"))
    server, app = make_server(config)
    host, port = server.server_address[:2]
    signal.signal(signal.SIGTERM, lambda signum, _frame: request_stop(server, app, signal.Signals(signum).name))
    log.event("process.start", host=host, port=port, backend=config.backend, pid=os.getpid())
    for orphan in app.orphans:
        print(f"⚠ погашен процесс прошлого запуска: {' '.join(orphan['argv'])[:120]}")
    print(f"ai-platform: http://{host}:{port}  (backend: {config.backend})", flush=True)
    print(f"данные: {config.data_dir}  лог: {log_path}")
    for module in app.modules.values():
        print(f"  вкладка {module.label}: http://{host}:{port}/{module.prefix}/")
    for problem in app.problems():
        print(f"⚠ {problem}")
        log.event("problem", level=logging.WARNING, error=problem)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаюсь…")
        app.stopping = True
    finally:
        started = time.monotonic()
        server.server_close()
        app.close()
        log.event("process.stop", duration_s=round(time.monotonic() - started, 1))
        log.close()


if __name__ == "__main__":
    main()
