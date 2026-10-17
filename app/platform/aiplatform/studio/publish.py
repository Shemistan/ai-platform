"""Каналы публикации: Telegram (Bot API) и пакет для ручной загрузки.

Список каналов — channels.json (путь AIP_CHANNELS), пример —
channels.example.json. Пока файла нет, каналы — пакеты для ручной загрузки
плюс каналы Telegram из telegram_tems.md narrator (строки ИМЯ=id; id вида
-1001234567890, -1001234567890_3 — тема в группе — или @username).
Добавленный со страницы канал записывает channels.json целиком.

Токен бота: из переменной канала (token_env), иначе — как у narrator:
$NARRATOR_TELEGRAM_TOKEN, файл $NARRATOR_TELEGRAM_TOKEN_FILE или bot_token.md
narrator. В файлы, лог и ответы API он не попадает. YouTube / TikTok /
Instagram требуют OAuth-приложений — для них канал типа manual: готовый
mp4 + заголовок + описание в папке.
"""

from __future__ import annotations

import html
import json
import os
import random
import re
import shutil
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from aiplatform.core.fsutil import atomic_write_json
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
_CHAT = re.compile(r"-?\d+(?:_\d+)?|@\w+")  # как resolve_channel у narrator: канал, тема в группе, @username
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
NARRATOR_CHANNELS = "telegram_tems.md"
NARRATOR_TOKEN_ENV = "NARRATOR_TELEGRAM_TOKEN"
NARRATOR_TOKEN_FILE_ENV = "NARRATOR_TELEGRAM_TOKEN_FILE"
NARRATOR_TOKEN_FILE = "bot_token.md"
GENERAL_THREAD = 1  # тема General: в неё пишут без message_thread_id


@dataclass(frozen=True)
class Channel:
    id: str
    label: str
    type: str
    format: str = LANDSCAPE
    chat_id: str = ""
    token_env: str = ""
    token_file: str = ""  # bot_token.md narrator — если у канала нет своей переменной

    def public(self) -> dict:
        data = asdict(self)
        data.pop("token_file")
        data["ready"] = self.problem() is None
        data["problem"] = self.problem()
        return data

    def stored(self) -> dict:
        """Как канал записывается в channels.json."""
        data = {k: v for k, v in asdict(self).items() if v and k != "token_file"}
        return data | {"format": self.format}

    def token(self) -> str:
        """Токен бота; пусто — не найден."""
        if self.token_env:
            return os.environ.get(self.token_env, "").strip()
        if os.environ.get(NARRATOR_TOKEN_ENV, "").strip():
            return os.environ[NARRATOR_TOKEN_ENV].strip()
        path = os.environ.get(NARRATOR_TOKEN_FILE_ENV) or self.token_file
        try:
            found = _TOKEN.search(Path(path).read_text(encoding="utf-8")) if path else None
        except OSError:
            found = None
        return found.group(0) if found else ""

    def target(self) -> dict[str, str]:
        """chat_id и, если канал — тема в группе (`<id>_<тема>`), message_thread_id."""
        chat, _, thread = self.chat_id.partition("_") if not self.chat_id.startswith("@") else (self.chat_id, "", "")
        if thread and int(thread) != GENERAL_THREAD:
            return {"chat_id": chat, "message_thread_id": thread}
        return {"chat_id": chat}

    def problem(self) -> str | None:
        if self.type == TELEGRAM:
            if not self.chat_id:
                return "не указан chat_id"
            if not _CHAT.fullmatch(self.chat_id):
                return "chat_id — вида -1001234567890, -1001234567890_3 (тема) или @username"
            if not self.token():
                if self.token_env:
                    return f"нет токена в переменной {self.token_env}"
                return f"нет токена бота: ${NARRATOR_TOKEN_ENV} или {NARRATOR_TOKEN_FILE} в narrator"
        return None


def narrator_channels(narrator_dir: Path) -> list[dict]:
    """Каналы Telegram из telegram_tems.md narrator: строки ИМЯ=id, пустые и с # пропускаются."""
    path = narrator_dir / NARRATOR_CHANNELS
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for n, line in enumerate(lines, start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = (part.strip() for part in line.partition("="))
        if not _NAME.fullmatch(name) or not _CHAT.fullmatch(value):
            raise ValueError(f"{path}:{n}: ждём ИМЯ=id, id вида -1001234567890, -1001234567890_3 или @username")
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        out.append({"id": f"tg-{slug}", "label": f"Telegram: {name}", "type": TELEGRAM, "chat_id": value})
    return out


def load_channels(path: Path, narrator_dir: Path | None = None) -> list[Channel]:
    """Каналы из файла; нет файла — пакеты для ручной загрузки и каналы из telegram_tems.md narrator."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = list(DEFAULT_CHANNELS) + (narrator_channels(narrator_dir) if narrator_dir else [])
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path}: не читается как JSON ({exc})") from exc
    token_file = str(narrator_dir / NARRATOR_TOKEN_FILE) if narrator_dir else ""
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
                token_file=token_file,
            )
        )
    if len({c.id for c in out}) != len(out):
        raise ValueError(f"{path}: id каналов повторяются")
    return out


def save_channels(path: Path, channels: list[Channel]) -> None:
    """Записать список целиком. Зовёт только ChannelStore — под своим замком (channels/store.py)."""
    atomic_write_json(path, [c.stored() for c in channels])


def new_channel(existing: list[Channel], label: str, chat_id: str, fmt: str) -> Channel:
    """Канал Telegram со страницы: id из chat_id, проверки как у narrator."""
    label, chat_id = " ".join(label.split())[:80], chat_id.strip()
    if not label:
        raise ValueError("Назови канал.")
    if not _CHAT.fullmatch(chat_id):
        raise ValueError("id канала — вида -1001234567890, -1001234567890_3 (тема в группе) или @username.")
    if fmt not in (LANDSCAPE, VERTICAL):
        raise ValueError("Формат — landscape или vertical.")
    if any(c.type == TELEGRAM and c.chat_id == chat_id and c.format == fmt for c in existing):
        raise ValueError("Такой канал уже есть.")
    base = "tg-" + (re.sub(r"[^a-z0-9]+", "-", chat_id.lower()).strip("-") or "channel")
    cid, n = base[:60], 2
    while any(c.id == cid for c in existing):
        cid, n = f"{base[:57]}-{n}", n + 1
    return Channel(id=cid, label=label, type=TELEGRAM, format=fmt, chat_id=chat_id)


def edit_channel(existing: list[Channel], channel_id: str, changes: dict) -> list[Channel]:
    """Название, формат и переменная токена канала; chat_id не меняется — это уже другой канал."""
    out = []
    found = False
    for c in existing:
        if c.id != channel_id:
            out.append(c)
            continue
        found = True
        label = " ".join(str(changes.get("label", c.label)).split())[:80]
        fmt = str(changes.get("format", c.format))
        token_env = str(changes.get("token_env", c.token_env) or "").strip()
        if not label:
            raise ValueError("Назови канал.")
        if fmt not in (LANDSCAPE, VERTICAL):
            raise ValueError("Формат — landscape или vertical.")
        if token_env and not re.fullmatch(r"[A-Z_][A-Z0-9_]*", token_env):
            raise ValueError("Переменная токена — латиницей заглавными, вида AIP_TELEGRAM_TOKEN.")
        out.append(Channel(c.id, label, c.type, fmt, c.chat_id, token_env, c.token_file))
    if not found:
        raise LookupError("Нет такого канала.")
    return out


def from_narrator(names: dict[str, str]) -> list[dict]:
    """Каналы движка (имя → id из telegram_tems.md) в виде записей channels.json."""
    out = []
    for name, chat in names.items():
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
        out.append({"id": f"tg-{slug}", "label": f"Telegram: {name}", "type": TELEGRAM, "chat_id": chat})
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


# Неудаляемый хвост подписи: сноски реестра («* Компания … признана …») и строки об источнике.
_TAIL_LINE = re.compile(r"^(?:\*\s|\**(?:Источник|Оригинал|Автор)\b)")
# Где можно резать тело: после конца предложения или абзаца; запасной вариант — по пробелу.
_SENTENCE_END = re.compile(r"[.!?…»)](?=\s)|\n")
_SPACE = re.compile(r"\s+")


def split_tail(post: str) -> tuple[str, str]:
    """(тело, хвост): хвост — последние абзацы из сносок и строк «Источник/Оригинал/Автор»."""
    paras = re.split(r"\n\s*\n", post.strip())
    cut = len(paras)
    while cut > 0 and all(_TAIL_LINE.match(line.strip()) for line in paras[cut - 1].splitlines() if line.strip()):
        cut -= 1
    return "\n\n".join(paras[:cut]).strip(), "\n\n".join(paras[cut:]).strip()


def visible_len(body: str) -> int:
    """Сколько знаков подписи видит Telegram: без тегов, с раскрытыми сущностями."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", body)))


def _spans(text: str) -> list[tuple[int, int]]:
    """Где в тексте разметка: **жирный** и [ссылка](url) — внутри неё резать нельзя."""
    return [(m.start(), m.end()) for rx in (_LINK, _BOLD) for m in rx.finditer(text)]


def _cut_points(text: str) -> tuple[list[int], list[int]]:
    """Позиции, где можно оборвать текст, не разрезав **жирный** или [ссылку](url): концы предложений и пробелы."""
    inside = _spans(text)

    def free(pos: int) -> bool:
        return not any(a < pos < b for a, b in inside)

    sentences = [m.end() for m in _SENTENCE_END.finditer(text) if free(m.end())]
    words = [m.start() for m in _SPACE.finditer(text) if free(m.start())]
    return sentences, words


def caption(post: str, title: str) -> str:
    """Подпись не длиннее лимита Telegram. Длинный пост режется по предложению, хвост (сноски
    о запрещённых организациях, источник) остаётся целиком; не влезает и он — StudioError."""
    if not post.strip():
        return f"<b>{html.escape(title)}</b>"
    full = post_html(post)
    if visible_len(full) <= CAPTION_LIMIT:
        return full
    body, tail = split_tail(post)
    tail_html = post_html(tail) if tail else ""
    budget = CAPTION_LIMIT - (visible_len(tail_html) + 2 if tail_html else 0)
    sentences, words = _cut_points(body)
    spans = _spans(body)
    hard = [n for n in range(min(len(body), budget), 0, -1) if n not in words]  # одно слово длиннее лимита
    candidates = [(n, "") for n in sorted(sentences, reverse=True)]
    candidates += [(n, "…") for n in sorted(words, reverse=True)] + [(n, "…") for n in hard]
    for n, mark in candidates:
        head = body[:n].rstrip()
        if not head or any(a < n < b for a, b in spans):
            continue
        head_html = post_html(head) + mark
        if visible_len(head_html) <= budget:
            return f"{head_html}\n\n{tail_html}" if tail_html else head_html
    raise StudioError(
        f"Подпись не влезает в {CAPTION_LIMIT} знаков Telegram: сноски о запрещённых организациях "
        "и источник занимают весь лимит — сократи пост или их текст."
    )


def audio_caption(post: str, title: str) -> str:
    """Короткая подпись к аудио: заголовок и неудаляемый хвост поста (сноски, источник)."""
    name = title.replace("*", "").strip()
    tail = split_tail(post)[1]
    head = f"**{name}**" if name else ""
    return caption(f"{head}\n\n{tail}".strip(), title)


# --- отправка ---

HttpPost = Callable[[str, bytes, str], dict]
FILE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".m4a": "audio/mp4",
    ".wav": "audio/wav",
}
PHOTO_LIMIT = 10 * 2**20  # sendPhoto принимает картинки до 10 МБ
POST_FORMATS = ("video", "photo")  # видео одним постом или картинка с подписью, затем аудио


class Unknown(StudioError):  # noqa: N818 — не отказ, а «неизвестно, ушёл ли пост»
    """Запрос ушёл, а ответа нет (таймаут, обрыв): Telegram мог опубликовать пост.
    Повторять сам нельзя — решает человек, проверив канал."""


def _http_post(url: str, body: bytes, content_type: str) -> dict:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read() or b"{}")
        except json.JSONDecodeError:
            return {"ok": False, "error_code": exc.code, "description": f"HTTP {exc.code}"}
    except urllib.error.URLError as exc:
        # urllib заворачивает в URLError сбои соединения и отправки запроса: целиком он не дошёл.
        raise StudioError(f"Telegram недоступен: {exc.__class__.__name__}") from exc
    except OSError as exc:
        # Запрос отправлен, ответа нет (таймаут чтения, обрыв) — пост мог уйти.
        raise Unknown(f"Telegram не ответил ({exc.__class__.__name__}) — пост мог уйти") from exc
    try:
        return json.loads(raw or b"{}")
    except json.JSONDecodeError:
        # Ответ 200 принят, но не разобран: пост ушёл, id сообщения неизвестен.
        return {"ok": True, "result": {}}


# Повторы запросов к Bot API. Методы о канале (getChat, getChatMemberCount) повторяем на сетевые
# сбои, 5xx и 429; отправку — только на 429 и на сбой до отправки тела (StudioError): после
# таймаута ответа (Unknown) пост мог уйти, повтор дал бы дубль — решает человек (BE-001).
ATTEMPTS = 3
BACKOFF_S = 1.0  # пауза перед вторым заходом; дальше вдвое больше, с разбросом
MAX_RETRY_AFTER_S = 120  # Telegram просит ждать дольше — не ждём, отдаём ошибку
_sleep = time.sleep  # тесты подменяют, чтобы паузы были мгновенными


def backoff(attempt: int) -> float:
    """1, 2, 4… с, с разбросом ±50% — повторы разных выпусков не бьют разом."""
    return BACKOFF_S * 2 ** (attempt - 1) * (0.5 + random.random())


def call_api(url: str, body: bytes, ctype: str, http_post: HttpPost | None, *, safe: bool) -> dict:
    """Запрос к Bot API с безопасными повторами; safe — метод только читает (повтор не создаёт пост)."""
    post = http_post or _http_post
    for attempt in range(1, ATTEMPTS + 1):
        last = attempt == ATTEMPTS
        try:
            answer = post(url, body, ctype)
        except Unknown:
            if not safe or last:
                raise
            _sleep(backoff(attempt))
            continue
        except StudioError:  # запрос не ушёл целиком — повтор безопасен и для отправки
            if last:
                raise
            _sleep(backoff(attempt))
            continue
        if answer.get("ok") or last:
            return answer
        code = answer.get("error_code")
        params = answer.get("parameters") if isinstance(answer.get("parameters"), dict) else {}
        wait = params.get("retry_after") or 0
        if code == 429 and isinstance(wait, int | float) and wait <= MAX_RETRY_AFTER_S:
            _sleep(wait or backoff(attempt))
            continue
        if safe and isinstance(code, int) and code >= 500:
            _sleep(backoff(attempt))
            continue
        return answer
    raise AssertionError("недостижимо")


def multipart(fields: dict[str, str], files: dict[str, Path]) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
    for name, path in files.items():
        ctype = FILE_TYPES.get(path.suffix.lower(), "application/octet-stream")
        head = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        )
        parts.append(head.encode() + path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _send(channel: Channel, method: str, fields: dict, files: dict[str, Path], http_post: HttpPost | None) -> dict:
    token = channel.token()
    body, ctype = multipart({**channel.target(), **fields}, files)
    answer = call_api(f"{TELEGRAM_API}/bot{token}/{method}", body, ctype, http_post, safe=False)
    if not answer.get("ok"):
        description = str(answer.get("description", "неизвестная ошибка")).replace(token, "***")
        raise StudioError(f"{channel.label}: Telegram ответил «{description}».")
    return answer.get("result") or {}


def chat_call(channel: Channel, method: str, http_post: HttpPost | None = None) -> dict | int | None:
    """Метод Bot API о самом канале (getChat, getChatMemberCount); сбой — None, публикацию он не держит."""
    token = channel.token()
    if not token:
        return None
    body, ctype = multipart({"chat_id": channel.target()["chat_id"]}, {})
    try:
        answer = call_api(f"{TELEGRAM_API}/bot{token}/{method}", body, ctype, http_post, safe=True)
    except StudioError:
        return None
    return answer.get("result") if answer.get("ok") else None


def members(channel: Channel, http_post: HttpPost | None = None) -> int | None:
    found = chat_call(channel, "getChatMemberCount", http_post)
    return found if isinstance(found, int) else None


PHOTO_PARTS = ("photo", "audio")  # пост «картинка и аудио» — два сообщения, отправляются по очереди


def photo_problem(channel: Channel, poster: Path, audio: Path) -> str | None:
    """Почему пост «картинка и аудио» не уйдёт — до отправки первой части."""
    problem = channel.problem()
    if problem:
        return f"{channel.label}: {problem}."
    if poster.stat().st_size > PHOTO_LIMIT:
        return f"{channel.label}: картинка больше 10 МБ — Telegram не примет её фото."
    if audio.stat().st_size > BOT_VIDEO_LIMIT:
        return f"{channel.label}: аудио больше 50 МБ — Bot API такое не примет."
    return None


def send_photo_part(
    channel: Channel,
    part: str,
    *,
    poster: Path,
    audio: Path,
    title: str,
    post: str,
    seconds: float = 0.0,
    http_post: HttpPost | None = None,
) -> int | None:
    """Одна часть поста «картинка и аудио»; возвращает id сообщения."""
    if part == "photo":
        fields = {"caption": caption(post, title), "parse_mode": "HTML"}
        return _send(channel, "sendPhoto", fields, {"photo": poster}, http_post).get("message_id")
    # Аудио могут переслать отдельно от картинки — сноски реестра и источник идут и в его подписи.
    fields = {"title": title[:64], "caption": audio_caption(post, title), "parse_mode": "HTML"}
    if seconds:
        fields["duration"] = str(int(round(seconds)))
    return _send(channel, "sendAudio", fields, {"audio": audio}, http_post).get("message_id")


def photo_result(channel: Channel, ids: dict[str, int | None]) -> dict:
    return {
        "channel": channel.id,
        "ok": True,
        "message": f"Отправлено двумя постами: картинка и аудио, сообщения {ids.get('photo')} и {ids.get('audio')}.",
        "message_id": ids.get("photo"),
        "chat_id": channel.target()["chat_id"],
    }


def publish_photo(
    channel: Channel,
    *,
    poster: Path,
    audio: Path,
    title: str,
    post: str,
    seconds: float = 0.0,
    http_post: HttpPost | None = None,
) -> dict:
    """Двумя постами, как NEWS_POST=photo у narrator: картинка с подписью, затем аудио."""
    problem = photo_problem(channel, poster, audio)
    if problem:
        raise StudioError(problem)
    ids = {
        part: send_photo_part(
            channel, part, poster=poster, audio=audio, title=title, post=post, seconds=seconds, http_post=http_post
        )
        for part in PHOTO_PARTS
    }
    return photo_result(channel, ids)


def publish(
    channel: Channel,
    *,
    video: Path,
    title: str,
    post: str,
    out_dir: Path,
    http_post: HttpPost | None = None,
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
    token = channel.token()
    fields = {
        **channel.target(),
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
    answer = call_api(f"{TELEGRAM_API}/bot{token}/sendVideo", body, ctype, http_post, safe=False)
    if not answer.get("ok"):
        description = str(answer.get("description", "неизвестная ошибка")).replace(token, "***")
        raise StudioError(f"{channel.label}: Telegram ответил «{description}».")
    message_id = (answer.get("result") or {}).get("message_id")
    # id сообщения — чтобы потом подтянуть просмотры.
    return {
        "channel": channel.id,
        "ok": True,
        "message": f"Отправлено, сообщение {message_id}.",
        "message_id": message_id,
        "chat_id": channel.target()["chat_id"],
    }
