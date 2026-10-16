"""HTTP-сервер платформы: вкладки-модули под /api/<prefix>/, статика, общая шапка.

Запуск: `python3 -m aiplatform.server` (или `make run` / `make demo`).
"""

from __future__ import annotations

import mimetypes
from collections.abc import Callable
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from aiplatform import config as config_module
from aiplatform.config import Config
from aiplatform.core import media
from aiplatform.core.gate import Gate
from aiplatform.core.http import HttpError, Request, not_found
from aiplatform.core.uploads import UploadStore

STATIC_DIR = Path(__file__).with_name("static")
HOME = "/photo/"


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
    """Общее для модулей: настройки, GPU-замок, загрузки, соседние модули."""

    config: Config
    gate: Gate
    uploads: UploadStore
    modules: dict[str, Module] = field(default_factory=dict)


ModulesFactory = Callable[[Context], list[Module]]


def default_modules(ctx: Context) -> list[Module]:
    """Все вкладки платформы в порядке шапки; сценарии — после вкладок, которыми пользуются."""
    from aiplatform.audio.service import AudioModule
    from aiplatform.photo.service import PhotoModule
    from aiplatform.studio.service import StudioModule
    from aiplatform.updates.service import UpdatesModule
    from aiplatform.video.service import VideoModule

    return [PhotoModule(ctx), VideoModule(ctx), AudioModule(ctx), StudioModule(ctx), UpdatesModule(ctx)]


class App:
    def __init__(self, config: Config, modules: ModulesFactory = default_modules) -> None:
        self.config = config
        self.ctx = Context(config=config, gate=Gate(), uploads=UploadStore(config.uploads_dir))
        for module in modules(self.ctx):
            self.ctx.modules[module.prefix] = module
        for module in self.ctx.modules.values():  # модулю нужны соседи — они уже созданы
            bind = getattr(module, "bind", None)
            if bind:
                bind()

    @property
    def modules(self) -> dict[str, Module]:
        return self.ctx.modules

    def problems(self) -> list[str]:
        out = []
        if not media.ffmpeg_available(self.config.ffmpeg):
            out.append("ffmpeg не найден: сборка видео и демо-генерация не работают — brew install ffmpeg.")
        if not self.config.font:
            out.append("Не найден шрифт с кириллицей для слайдов — задай AIP_FONT=/путь/к/шрифту.ttf.")
        for module in self.modules.values():
            out.extend(module.problems())
        return out

    def describe(self) -> dict:
        return {
            "backend": self.config.backend,
            "tabs": [{"id": m.prefix, "label": m.label} for m in self.modules.values()],
            "problems": self.problems(),
        }

    def status(self) -> dict:
        return {"gpu": self.ctx.gate.holder(), "waiting": self.ctx.gate.waiting()}

    def close(self) -> None:
        for module in reversed(list(self.modules.values())):
            module.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "aiplatform"
    protocol_version = "HTTP/1.1"
    app: App  # задаётся в make_server

    def log_message(self, fmt: str, *args: object) -> None:  # тишина: опрос статуса шумит
        pass

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET", head=True)

    def do_POST(self) -> None:
        self._dispatch("POST")

    def _dispatch(self, method: str, *, head: bool = False) -> None:
        url = urlsplit(self.path)
        parts = [p for p in url.path.split("/") if p]
        req = Request(self, method, parts, parse_qs(url.query), head=head)
        try:
            # Чужая страница в браузере не должна запускать генерацию на localhost.
            origin = self.headers.get("Origin")
            if method == "POST" and origin and urlsplit(origin).netloc != self.headers.get("Host"):
                raise HttpError(HTTPStatus.FORBIDDEN, "Запрос с чужого сайта отклонён.")
            if parts[:1] != ["api"]:
                if method != "GET":
                    raise HttpError(HTTPStatus.METHOD_NOT_ALLOWED, "Метод не поддерживается.")
                return self._static(req, url.path, parts)
            match (method, parts[1:]):
                case ("GET", ["config"]):
                    return req.send_json(self.app.describe())
                case ("GET", ["status"]):
                    return req.send_json(self.app.status())
                case (_, [prefix, *rest]) if prefix in self.app.modules:
                    req.sub = rest
                    return self.app.modules[prefix].handle(req)
            raise not_found()
        except HttpError as exc:
            req.send_json({"error": exc.message}, exc.status)
        except (BrokenPipeError, ConnectionResetError):
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


def main() -> None:
    config = config_module.load()
    server, app = make_server(config)
    host, port = server.server_address[:2]
    print(f"ai-platform: http://{host}:{port}  (backend: {config.backend})")
    print(f"данные: {config.data_dir}")
    for module in app.modules.values():
        print(f"  вкладка {module.label}: http://{host}:{port}/{module.prefix}/")
    for problem in app.problems():
        print(f"⚠ {problem}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nостанавливаюсь…")
    finally:
        server.server_close()
        app.close()


if __name__ == "__main__":
    main()
