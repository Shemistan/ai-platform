"""Каналы публикации: Telegram (Bot API) и пакет для ручной загрузки.

Список каналов — channels.json (путь AIP_CHANNELS), пример —
channels.example.json. Токен бота — только из переменной окружения,
имя которой указано в канале (token_env): в файлы, лог и ответы API он
не попадает. YouTube / TikTok / Instagram требуют OAuth-приложений —
для них канал типа manual: готовый mp4 + заголовок + описание в папке.
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from aiplatform.studio.llm import StudioError

TELEGRAM, MANUAL = "telegram", "manual"
LANDSCAPE, VERTICAL = "landscape", "vertical"
CAPTION_LIMIT = 1024
BOT_VIDEO_LIMIT = 50 * 2**20  # Bot API принимает файлы до 50 МБ
TELEGRAM_API = "https://api.telegram.org"
DEFAULT_CHANNELS = (
    {"id": "download-16x9", "label": "Файл для YouTube / VK (16:9)", "type": MANUAL, "format": LANDSCAPE},
    {"id": "download-9x16", "label": "Файл для Shorts / TikTok / Reels (9:16)", "type": MANUAL, "format": VERTICAL},
)
_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Channel:
    id: str
    label: str
    type: str
    format: str = LANDSCAPE
    chat_id: str = ""
    token_env: str = ""

    def public(self) -> dict:
        data = asdict(self)
        data["ready"] = self.problem() is None
        data["problem"] = self.problem()
        return data

    def problem(self) -> str | None:
        if self.type == TELEGRAM:
            if not self.chat_id:
                return "не указан chat_id"
            if not self.token_env:
                return "не указан token_env"
            if not os.environ.get(self.token_env):
                return f"нет токена в переменной {self.token_env}"
        return None


def load_channels(path: Path) -> list[Channel]:
    """Каналы из файла; нет файла — только пакеты для ручной загрузки."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = list(DEFAULT_CHANNELS)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: не читается как JSON ({exc})") from exc
    if not isinstance(raw, list):
        raise ValueError(f"{path}: ожидается список каналов")
    out = []
    for n, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"{path}: канал {n} — не объект")
        kind = item.get("type")
        fmt = item.get("format", LANDSCAPE)
        cid = str(item.get("id", ""))
        if kind not in (TELEGRAM, MANUAL) or fmt not in (LANDSCAPE, VERTICAL) or not _ID.match(cid):
            raise ValueError(
                f"{path}: канал {n} — нужны id (латиница), type telegram|manual, format landscape|vertical"
            )
        out.append(
            Channel(
                id=cid,
                label=str(item.get("label") or cid),
                type=kind,
                format=fmt,
                chat_id=str(item.get("chat_id", "")),
                token_env=str(item.get("token_env", "")),
            )
        )
    return out


# --- подпись: **жирный** и [текст](url) → HTML Telegram ---


def post_html(text: str) -> str:
    out = []
    for line in text.strip().splitlines():
        pieces, last = [], 0
        for m in _LINK.finditer(line):
            pieces.append(_bold(html.escape(line[last : m.start()], quote=False)))
            label = _bold(html.escape(m.group(1), quote=False))
            pieces.append(f'<a href="{html.escape(m.group(2))}">{label}</a>')
            last = m.end()
        pieces.append(_bold(html.escape(line[last:], quote=False)))
        out.append("".join(pieces).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _bold(escaped: str) -> str:
    return _BOLD.sub(r"<b>\1</b>", escaped)


def plain_text(text: str) -> str:
    """Описание для ручной загрузки: ссылки «текст (url)», без **."""
    return _LINK.sub(r"\1 (\2)", text).replace("**", "").strip()


def caption(post: str, title: str) -> str:
    """Подпись не длиннее лимита Telegram; не влезает — заголовок вместо поста."""
    body = post_html(post) if post.strip() else f"<b>{html.escape(title)}</b>"
    visible = len(html.unescape(re.sub(r"<[^>]+>", "", body)))
    if visible <= CAPTION_LIMIT:
        return body
    return f"<b>{html.escape(title[: CAPTION_LIMIT - 10])}</b>"


# --- отправка ---

HttpPost = Callable[[str, bytes, str], dict]


def _http_post(url: str, body: bytes, content_type: str) -> dict:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except (urllib.error.URLError, OSError) as exc:
        raise StudioError(f"Telegram недоступен: {exc.__class__.__name__}") from exc


def multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, path in files.items():
        ctype = "image/jpeg" if path.suffix.lower() in (".jpg", ".jpeg") else "video/mp4"
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        )
        parts.append(head.encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def publish(
    channel: Channel,
    *,
    video: Path,
    title: str,
    post: str,
    out_dir: Path,
    http_post: HttpPost = _http_post,
    width: int = 0,
    height: int = 0,
    seconds: float = 0.0,
    cover: Path | None = None,
    thumb: Path | None = None,
) -> dict:
    """Отправить видео в канал; результат — {channel, ok, message, files?}.

    cover и thumb — обложка видео (первый кадр с плеем и плашкой) и её миниатюра:
    без них Telegram сам берёт случайный кадр.
    """
    if channel.type == MANUAL:
        target = out_dir / channel.id
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(video, target / "video.mp4")
        (target / "title.txt").write_text(title.strip() + "\n", encoding="utf-8")
        (target / "description.txt").write_text(plain_text(post) + "\n", encoding="utf-8")
        return {
            "channel": channel.id,
            "ok": True,
            "message": "Пакет готов: видео, заголовок и описание — загрузи вручную.",
            "files": [f"publish/{channel.id}/{name}" for name in ("video.mp4", "title.txt", "description.txt")],
        }
    problem = channel.problem()
    if problem:
        raise StudioError(f"{channel.label}: {problem}.")
    if video.stat().st_size > BOT_VIDEO_LIMIT:
        raise StudioError(f"{channel.label}: видео больше 50 МБ — Bot API такое не примет.")
    token = os.environ[channel.token_env]
    fields = {
        "chat_id": channel.chat_id,
        "caption": caption(post, title),
        "parse_mode": "HTML",
        "supports_streaming": "true",
    }
    if width and height:
        fields |= {"width": str(width), "height": str(height)}
    if seconds:
        fields["duration"] = str(int(round(seconds)))
    files = {"video": video}
    if cover is not None:
        fields["cover"] = "attach://cover"
        files["cover"] = cover
    if thumb is not None:
        files["thumbnail"] = thumb
    body, ctype = multipart(fields, files)
    answer = http_post(f"{TELEGRAM_API}/bot{token}/sendVideo", body, ctype)
    if not answer.get("ok"):
        description = str(answer.get("description", "неизвестная ошибка")).replace(token, "***")
        raise StudioError(f"{channel.label}: Telegram ответил «{description}».")
    message_id = (answer.get("result") or {}).get("message_id")
    return {"channel": channel.id, "ok": True, "message": f"Отправлено, сообщение {message_id}."}
