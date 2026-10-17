"""HTTP-сервер платформы: вкладки-модули под /api/<prefix>/, статика, общая шапка.

Запуск: `python3 -m aiplatform.server` (или `make run` / `make demo`).
"""

from __future__ import annotations

import hmac
import html
import logging
import mimetypes
import os
import re
import shutil
import signal
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlencode, urlsplit

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
AUTH_COOKIE = "aip_auth"
# Служебный заголовок запросов страниц платформы (ui.js api()): чужой сайт поставить
# его не может без CORS-preflight, а preflight сервер не пропускает (SEC-002).
CSRF_HEADER = "X-AIP"
CSP = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",  # style="…" в разметке и h(…, {style})
        "img-src 'self' blob: data:",
        "media-src 'self' blob: data:",
        "connect-src 'self'",
        "font-src 'self' data:",
        "object-src 'none'",
        "base-uri 'none'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    )
)
SECURITY_HEADERS = (
    ("X-Frame-Options", "DENY"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Content-Security-Policy", CSP),
)
NO_AUTH = (("GET", ("api", "health")), ("GET", ("api", "ready")))  # пробы живости — без токена
LOGIN_PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Вход — AI Platform</title></head>
<body style="font-family:system-ui,sans-serif;max-width:420px;margin:15vh auto;padding:0 16px">
<h1>AI Platform</h1><p>Нужен токен доступа (AIP_AUTH_TOKEN).</p>
<form method="get" action="{action}"><input type="password" name="token" autocomplete="current-password"
 aria-label="Токен" style="width:100%;padding:8px" autofocus><p><button type="submit">Войти</button></p></form>
</body></html>
"""


def allowed_hosts(config: Config, port: int) -> frozenset[str]:
    """Допустимые значения Host: loopback, адрес из AIP_HOST и AIP_ALLOWED_HOSTS; без порта — свой порт."""
    names = {"127.0.0.1", "localhost", "[::1]", *config.allowed_hosts}
    if config.host and config.host not in ("0.0.0.0", "::"):
        names.add(config.host)
    out = set()
    for name in names:
        name = name.strip().lower()
        if ":" in name and not name.startswith("[") and name.count(":") > 1:
            name = f"[{name}]"  # голый IPv6
        out.add(name if re.search(r":\d+$", name) else f"{name}:{port}")
    return frozenset(out)


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
            # Очереди вкладок: задачи в карантине или записанные более новой версией (BE-008).
            for queue in (getattr(module, "jobs", None), getattr(module, "tasks", None)):
                out.extend(getattr(queue, "load_problems", None) or [])
        return list(dict.fromkeys(out))  # общий движок (narrator) жалуется из нескольких вкладок

    def describe(self) -> dict:
        return {
            "backend": self.config.backend,
            "version": __version__,
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
    hosts: frozenset[str] = frozenset()  # задаётся в make_server, когда известен порт

    def end_headers(self) -> None:
        # Защитные заголовки — на всех ответах: JSON, статика, редиректы, ошибки (SEC-002).
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        super().end_headers()

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
            self._check_host()
            if not self._check_auth(req, method, url, parts):
                return
            if method == "POST":
                self._check_origin()
            if method == "POST" and self.app.stopping:
                stopping = "Сервис останавливается — повтори после перезапуска."
                raise HttpError(HTTPStatus.SERVICE_UNAVAILABLE, stopping, code="stopping")
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
                    if ready["ok"]:
                        return req.send_json(ready)
                    # 503 — тоже ответ-ошибка: code есть в каждом (BE-011), отчёт проверок — рядом.
                    return req.send_json(ready | {"code": "not_ready"}, HTTPStatus.SERVICE_UNAVAILABLE)
                case (_, [prefix, *rest]) if prefix in self.app.modules:
                    req.sub = rest
                    return self.app.modules[prefix].handle(req)
            raise not_found()
        except HttpError as exc:
            req.send_json(exc.body(), exc.status, exc.headers)
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

    def _check_host(self) -> None:
        """DNS rebinding: страница атакующего ходит к нам с его именем в Host — отказ."""
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in self.hosts:
            raise HttpError(
                HTTPStatus.MISDIRECTED_REQUEST,
                "Неизвестный адрес в Host. Заходи по 127.0.0.1 или localhost; свой адрес — в AIP_ALLOWED_HOSTS.",
            )

    def _check_origin(self) -> None:
        """Чужая страница в браузере не должна запускать генерацию и публикацию."""
        origin = self.headers.get("Origin")
        if origin is not None:
            parts = urlsplit(origin)
            if parts.scheme not in ("http", "https") or parts.netloc.lower() not in self.hosts:
                raise HttpError(HTTPStatus.FORBIDDEN, "Запрос с чужого сайта отклонён.")
        elif self.headers.get(CSRF_HEADER) != "1":
            raise HttpError(HTTPStatus.FORBIDDEN, f"Запрос без Origin и без заголовка {CSRF_HEADER}: 1 отклонён.")

    def _token_ok(self, given: str) -> bool:
        token = self.app.config.auth_token
        return bool(given) and hmac.compare_digest(given.encode("utf-8"), token.encode("utf-8"))

    def _check_auth(self, req: Request, method: str, url, parts: list[str]) -> bool:
        """Токен доступа (AIP_AUTH_TOKEN): False — ответ уже отправлен (вход, редирект)."""
        if not self.app.config.auth_token or (method, tuple(parts)) in NO_AUTH:
            return True
        query = parse_qs(url.query)
        given = (query.get("token") or [""])[0]
        if given and method == "GET" and parts[:1] != ["api"]:
            if not self._token_ok(given):
                self._login(req, HTTPStatus.UNAUTHORIZED)
                return False
            # Ссылка /?token=… ставит cookie и уводит на адрес без токена.
            rest = urlencode([(k, v) for k, vs in query.items() if k != "token" for v in vs], doseq=True)
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", url.path + (f"?{rest}" if rest else ""))
            self.send_header(
                "Set-Cookie", f"{AUTH_COOKIE}={self.app.config.auth_token}; Path=/; HttpOnly; SameSite=Strict"
            )
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
        bearer = self.headers.get("Authorization") or ""
        if bearer.startswith("Bearer ") and self._token_ok(bearer[7:].strip()):
            return True
        try:
            cookie = SimpleCookie(self.headers.get("Cookie") or "").get(AUTH_COOKIE)
        except CookieError:
            cookie = None
        if cookie is not None and self._token_ok(cookie.value):
            return True
        if parts[:1] == ["api"]:
            raise HttpError(HTTPStatus.UNAUTHORIZED, "Нужен токен доступа: Authorization: Bearer … или вход по ссылке.")
        self._login(req, HTTPStatus.UNAUTHORIZED)
        return False

    def _login(self, req: Request, status: HTTPStatus) -> None:
        data = LOGIN_PAGE.replace("{action}", html.escape(urlsplit(self.path).path, quote=True)).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not req.head:
            self.wfile.write(data)

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
    handler.hosts = allowed_hosts(config, server.server_address[1])
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
    try:
        config = config_module.load()
    except config_module.ConfigError as exc:
        raise SystemExit(f"ai-platform не запущен: {exc}") from exc
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
