"""Телеграм-бот: рассылка готовых озвучек в канал или подписчикам.

Каждая озвученная статья уходит файлом mp3 с коротким описанием: что за
статья, о чём и сколько слушать. Получатель — канал, где бот админ.

Все каналы и темы записаны в одном файле — `telegram_tems.md` в корне
репозитория, по строке `ИМЯ=id`. Озвученные статьи идут в канал
`ARTICLE_CHANNEL`, дайджест новостей (`kind: digest` в meta.json) — в
`NEWS_CHANNEL`: картинка с текстом поста, затем аудио. На один запуск канал
меняют `$NARRATOR_TELEGRAM_CHANNEL` и `$NARRATOR_NEWS_CHANNEL` или параметр
`channel`: имя из файла, id или @username. Если канал выключен
(`$NARRATOR_TELEGRAM_CHANNEL=off`), озвучка идёт в личку подписчикам — тем,
кто нажал Start в боте. Канал может быть темой в группе:
`<id группы>_<id темы>`, как в адресе web.telegram.org (`-1003755616451_3`).
Список подписчиков и отметки об отправке лежат в
`library/telegram.json` — это личные данные, а не код, поэтому и живут в
библиотеке, которая не в git. О подписчике хранится только chat_id, дата
подписки и тема (CMP-007); /stop и блокировка бота удаляют запись (CMP-006).
В логах chat_id — маской `mask()`.

Токен бота берётся из `$NARRATOR_TELEGRAM_TOKEN`, иначе из файла
`$NARRATOR_TELEGRAM_TOKEN_FILE`, иначе из `bot_token.md` в корне репозитория.
В git токену нельзя — `bot_token.md` в .gitignore.

Сеть нужна только здесь: остальной narrator работает офлайн. Запросы к
api.telegram.org идут через `Bot.transport`, в тестах вместо него подставляют
фейк.
"""

from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import html
import json
import mimetypes
import os
import random
import re
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime, timedelta
from pathlib import Path

from narrator import audit, library, registry
from narrator.library import Entry, article_id, audio_name, format_duration

TOKEN_ENV = "NARRATOR_TELEGRAM_TOKEN"
TOKEN_FILE_ENV = "NARRATOR_TELEGRAM_TOKEN_FILE"
TOKEN_FILE = "bot_token.md"
CHANNELS_FILE = "telegram_tems.md"  # все каналы и темы: строки ИМЯ=id
CHANNEL_ENV = "NARRATOR_TELEGRAM_CHANNEL"
ARTICLE_CHANNEL = "HISTORY_CHAN"  # имя канала с озвученными статьями
NEWS_CHANNEL_ENV = "NARRATOR_NEWS_CHANNEL"
NEWS_CHANNEL = "VOICE_NEWS"  # имя канала с дайджестом новостей
STATE_NAME = "telegram.json"
SERVE_LOCK = "telegram.serve.lock"  # держит `bot serve`: getUpdates зовёт только он
API_ROOT = "https://api.telegram.org"
CAPTION_LIMIT = 1024
SIZE_LIMIT = 50 * 1024 * 1024  # предел Telegram на файл от бота
TIMEOUT = 120
POLL_TIMEOUT = 30
# Повторы запросов к Bot API. Методы чтения повторяем на сетевые сбои, 5xx и 429;
# отправку (send*) — только на 429 и на сбой до отправки тела: после таймаута
# ответа сообщение могло уйти, и повтор дал бы дубль в канале.
SAFE_METHODS = frozenset({"getMe", "getChat", "getChatMemberCount", "getUpdates"})
ATTEMPTS = 3
BACKOFF_S = 1.0  # пауза перед вторым заходом; дальше вдвое больше, с разбросом
MAX_RETRY_AFTER_S = 120  # просит ждать дольше — не ждём, отдаём ошибку
SEND_RATE = 25  # сообщений в секунду на бота: ниже лимита Telegram 30 msg/s

_TOKEN_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
_CHAT_RE = re.compile(r"-?\d{5,}(?:_\d+)?|@\w{4,}")
_NAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_OFF = ("", "off", "none", "no")
GENERAL_THREAD = 1  # тема General: в неё пишут без message_thread_id

PRIVACY_URL_ENV = "NARRATOR_PRIVACY_URL"
PRIVACY_URL = (  # короткая политика: что храним, зачем, сколько, как удалить (CMP-008)
    "https://github.com/Shemistan/ai-platform/blob/main/app/narrator/docs/privacy.md"
)


def privacy_url() -> str:
    return os.environ.get(PRIVACY_URL_ENV, "").strip() or PRIVACY_URL


def welcome() -> str:
    """Приветствие подписчику: что пришлёт бот, что хранит и как всё удалить."""
    return (
        "Привет! Это бот с озвучкой статей.\n\n"
        "Как только очередная статья превращается в аудио, он пришлёт mp3 и короткое "
        "описание: о чём статья и сколько слушать.\n\n"
        "Храню только номер этого чата, дату подписки и метку ссылки-приглашения — "
        "чтобы присылать озвучки. "
        "Имя и сообщения не сохраняю.\n"
        "/stop — отписаться и удалить эти данные.\n"
        f"Подробнее: {privacy_url()}"
    )


WELCOME = welcome()  # текст с адресом политики по умолчанию; в ответе — welcome()
GOODBYE = "Отписал и удалил ваши данные. Чтобы снова получать озвучки, пришли /start."
HINT = "Я только рассылаю озвучки статей. Команды: /start — подписаться, /stop — отписаться."
NEED_INVITE = "Подписка по приглашению: пришли /start <код> или открой ссылку-приглашение."
FULL = "Мест в рассылке пока нет."
# Защита от флуда (SEC-012): одному чату — не больше ответа раз в REPLY_INTERVAL_S,
# число подписчиков ограничено, подписку можно закрыть инвайт-кодом.
REPLY_INTERVAL_ENV = "NARRATOR_BOT_REPLY_INTERVAL"
REPLY_INTERVAL_S = 60.0
MAX_SUBSCRIBERS_ENV = "NARRATOR_BOT_MAX_SUBSCRIBERS"  # 0 — без ограничения
MAX_SUBSCRIBERS = 1000
INVITE_ENV = "NARRATOR_BOT_INVITE"  # пусто — /start без кода
# Источник подписки (AN-011): ссылка t.me/<бот>?start=<метка> — метка выпуска, откуда
# пришёл подписчик. С инвайт-кодом payload — «<код>__<метка>». Метка — только
# [A-Za-z0-9_-] до 64 знаков (правило Telegram).
# @-имя бота без @: задано — в посте статьи ссылка «подписаться».
BOT_USERNAME_ENV = "NARRATOR_BOT_USERNAME"
START_SEP = "__"
_START_PARAM = re.compile(r"[A-Za-z0-9_-]{1,64}")


class TelegramError(RuntimeError):
    """Телеграм не ответил или ответил ошибкой."""


class NotSent(TelegramError):  # noqa: N818 — не сбой ответа, а «запрос не ушёл»
    """Запрос не ушёл (соединение не открылось, тело не отправлено) — повтор безопасен."""


def read_token() -> str:
    """Токен бота из переменной окружения или файла; иначе TelegramError."""
    env = os.environ.get(TOKEN_ENV, "").strip()
    if env:
        return env
    path = Path(os.environ.get(TOKEN_FILE_ENV) or library.library_root().parent / TOKEN_FILE)
    if path.exists():
        found = _TOKEN_RE.search(path.read_text(encoding="utf-8"))
        if found:
            return found.group(0)
        raise TelegramError(f"{path}: токен не найден, ждём строку вида 123456:AA…")
    raise TelegramError(
        f"токен бота неизвестен: положи его в {path} или в ${TOKEN_ENV} (получить — у @BotFather)"
    )


def channels_path() -> Path:
    return library.library_root().parent / CHANNELS_FILE


def channel_name(name: str) -> str:
    """Имя канала без оглядки на регистр и на то, дефис в нём или подчёркивание."""
    return name.strip().upper().replace("-", "_")


def channels() -> dict[str, str]:
    """Каналы из `telegram_tems.md`: имя → id, как записан в файле.

    Строка — `ИМЯ=id`; пустые и начатые с `#` пропускаются. Кривая строка —
    TelegramError: молча слать не туда хуже.
    """
    path = channels_path()
    if not path.exists():
        raise TelegramError(
            f"каналы неизвестны: запиши их в {path} строками ИМЯ=id "
            f"(вида {ARTICLE_CHANNEL}=-1001234567890)"
        )
    out: dict[str, str] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, value = (part.strip() for part in line.partition("="))
        if not _NAME_RE.fullmatch(name) or not _CHAT_RE.fullmatch(value):
            raise TelegramError(
                f"{path}:{n}: {line!r} — ждём ИМЯ=id, id вида -1001234567890, "
                "-1001234567890_3 (тема в группе) или @username"
            )
        if channel_name(name) in out:
            raise TelegramError(f"{path}:{n}: канал {name} записан дважды")
        out[channel_name(name)] = value
    return out


def resolve_channel(value: str) -> tuple[int | str | None, int | None]:
    """Канал и тема в нём по имени из `telegram_tems.md`, id или @username.

    `-1003755616451_3` — группа с темами и id темы; без `_3` темы нет.
    `off` — канала нет: (None, None). Имени нет в файле — TelegramError.
    """
    raw = value.strip()
    if raw.lower() in _OFF:
        return None, None
    if _NAME_RE.fullmatch(raw):
        known = channels()
        if channel_name(raw) not in known:
            raise TelegramError(
                f"канала {raw} нет в {channels_path()}: есть {', '.join(known) or 'пусто'}"
            )
        raw = known[channel_name(raw)]
    if not re.fullmatch(r"-?\d+(?:_\d+)?|@\w+", raw):
        raise TelegramError(f"канал {raw!r}: ждём имя из {CHANNELS_FILE}, id или @username")
    topic = re.fullmatch(r"(-?\d+)_(\d+)", raw)
    if topic:
        return int(topic.group(1)), int(topic.group(2))
    return (int(raw) if re.fullmatch(r"-?\d+", raw) else raw), None


def _default_channel(entry: Entry | None) -> tuple[int | str | None, int | None]:
    """Куда материал идёт сам: дайджест — в канал новостей, статья — в канал озвучек."""
    digest = entry is not None and entry.kind == "digest"
    env, name = (NEWS_CHANNEL_ENV, NEWS_CHANNEL) if digest else (CHANNEL_ENV, ARTICLE_CHANNEL)
    return resolve_channel(os.environ.get(env, name))


def channel() -> int | str | None:
    """Канал для озвучек: chat_id, @username или None — слать подписчикам в личку."""
    return _default_channel(None)[0]


def mask(chat_id: int | str) -> str:
    """chat_id для логов: последние четыре цифры, остальное скрыто (CMP-007)."""
    text = str(chat_id)
    return f"chat …{text[-4:]}" if len(text) > 4 else "chat …"


@dataclass
class Subscriber:
    """Подписчик — только номер чата и дата: имени и истории не храним (CMP-007).

    Отписка и блокировка бота удаляют запись целиком (CMP-006), поэтому
    неактивных подписчиков нет.
    """

    chat_id: int
    added: str
    thread: int | None = None  # тема в группе, если получатель — канал-тема
    start_param: str | None = None  # откуда подписался: метка из /start <payload> (AN-011)

    def address(self) -> dict:
        """Куда слать: chat_id и, если это тема в группе, message_thread_id."""
        if self.thread in (None, GENERAL_THREAD):
            return {"chat_id": self.chat_id}
        return {"chat_id": self.chat_id, "message_thread_id": self.thread}


@dataclass
class State:
    """Подписчики, курсор getUpdates и отметки о разосланных озвучках."""

    offset: int = 0
    subscribers: list[Subscriber] = field(default_factory=list)
    # «<дата>-<название>_<движок>» → когда разослали
    sent: dict[str, str] = field(default_factory=dict)
    # Завершённые подписки без ПДн (AN-011): {"added", "source", "left"} — ни chat_id, ни имени.
    # Для retention по когортам: строка остаётся, даже когда запись подписчика удалена (CMP-006).
    departed: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> State:
        if not path.exists():
            return cls()
        data = json.loads(path.read_text(encoding="utf-8"))
        known = {f.name for f in fields(cls)}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"{path}: неизвестные поля {sorted(unknown)}")
        state = cls(subscribers=_subscribers(data.pop("subscribers", [])), **data)
        state._base = copy.deepcopy(state)
        return state

    def save(self, path: Path) -> None:
        """Атомарно (tmp + os.replace, права 600) и без потери чужих правок.

        Файл пишут и `bot serve`, и `bot send`, и `synth --send` из других процессов.
        Под flock на `<файл>.lock` файл перечитывается и сливается с тем, что
        поменял этот процесс с момента load: свой offset (максимум), свои отметки
        `sent` и подписчики поверх чужих. Чего этот процесс не трогал — берётся с диска.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with state_lock(path):
            disk = State.load(path) if path.exists() else State()
            merged = _merge(getattr(self, "_base", None) or State(), self, disk)
            tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
            try:
                # Сразу 0600: подписчики — личные данные, без окна с правами по umask (SEC-010).
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(json.dumps(asdict(merged), ensure_ascii=False, indent=2) + "\n")
                os.replace(tmp, path)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            self.offset, self.subscribers, self.sent, self.departed = (
                merged.offset,
                merged.subscribers,
                merged.sent,
                merged.departed,
            )
            self._base = copy.deepcopy(merged)

    def find(self, chat_id: int) -> Subscriber | None:
        return next((s for s in self.subscribers if s.chat_id == chat_id), None)

    def active(self) -> list[Subscriber]:
        """Кому слать в личку: все записанные — отписавшихся не храним."""
        return list(self.subscribers)

    def upsert(self, chat_id: int, start_param: str | None = None) -> bool:
        """Подписывает чат. True — если подписчик новый (в том числе вернулся после /stop:
        его прежняя запись удалена, это новая подписка со своей датой и источником, AN-011)."""
        if self.find(chat_id) is not None:
            return False
        self.subscribers.append(
            Subscriber(chat_id=chat_id, added=library.today(), start_param=start_param)
        )
        return True

    def remove(self, chat_id: int) -> bool:
        """Удаляет подписчика целиком (/stop, бот заблокирован, `bot forget`). True — был.

        Дата отписки (AN-011) запись не переживает: она уходит только в departed —
        строку {added, source, left} без chat_id и имени, связать её с человеком нечем."""
        gone = [s for s in self.subscribers if s.chat_id == chat_id]
        self.subscribers = [s for s in self.subscribers if s.chat_id != chat_id]
        for sub in gone:
            self.departed.append(
                {"added": sub.added, "source": sub.start_param, "left": library.today()}
            )
        return bool(gone)


def _subscribers(raw: list[dict]) -> list[Subscriber]:
    """Подписчики из файла. Старый формат (до CMP-006/007) читается так:

    имя, заметка и прочие лишние поля отбрасываются, неактивные (`active: false`)
    не загружаются — при следующем save их в файле уже нет.
    """
    known = {f.name for f in fields(Subscriber)}
    return [
        Subscriber(**{k: v for k, v in s.items() if k in known})
        for s in raw
        if s.get("active", True)
    ]


@contextlib.contextmanager
def state_lock(path: Path) -> Iterator[None]:
    """Межпроцессный замок файла состояния: flock на соседнем `<файл>.lock`."""
    lock = path.with_name(path.name + ".lock")
    with open(lock, "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _merge(base: State, mine: State, disk: State) -> State:
    """Трёхстороннее слияние: что процесс поменял относительно base — его, прочее — с диска."""
    out = State(offset=max(mine.offset, disk.offset))
    out.sent = dict(disk.sent)
    for key, stamp in mine.sent.items():
        if base.sent.get(key) != stamp:
            out.sent[key] = max(stamp, disk.sent.get(key, ""))
    was = {s.chat_id: s for s in base.subscribers}
    ours = {s.chat_id: s for s in mine.subscribers}
    subs = {s.chat_id: s for s in disk.subscribers}
    for chat_id in was.keys() - ours.keys():  # удалён здесь (/stop, блокировка, forget)
        subs.pop(chat_id, None)
    for chat_id, sub in ours.items():
        if was.get(chat_id) != sub:  # подписался здесь
            subs[chat_id] = sub
    out.subscribers = list(subs.values())
    # departed только дописывается: чужие строки с диска плюс свои новые с момента load.
    out.departed = list(disk.departed) + list(mine.departed[len(base.departed) :])
    return out


def start_source(payload: str, invite: str = "") -> tuple[str, str | None]:
    """/start <payload> → (инвайт-код, метка источника). С инвайтом payload — «<код>__<метка>»
    (или только код), без него весь payload — метка. Метка не по правилам Telegram — None."""
    code, source = payload.partition(START_SEP)[::2] if invite else ("", payload)
    return code, source if _START_PARAM.fullmatch(source or "") else None


def start_tag(text: str) -> str:
    """Метка источника из имени выпуска: латиница, цифры, _ и - до 64 знаков; иначе — хэш."""
    tag = re.sub(r"[^A-Za-z0-9_-]+", "-", text).strip("-")[:64]
    return tag or hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def start_link(source: str, username: str | None = None, invite: str | None = None) -> str | None:
    """Ссылка «подписаться» с меткой выпуска: t.me/<бот>?start=<метка>. Нет имени бота — None."""
    username = (
        (username if username is not None else os.environ.get(BOT_USERNAME_ENV, ""))
        .strip()
        .lstrip("@")
    )
    if not username:
        return None
    invite = (invite if invite is not None else os.environ.get(INVITE_ENV, "")).strip()
    payload = f"{invite}{START_SEP}{source}" if invite else source
    return f"https://t.me/{username}?start={payload}"


def _days(a: str, b: str) -> int:
    return (date.fromisoformat(b[:10]) - date.fromisoformat(a[:10])).days


def audience(state: State, today: str | None = None) -> dict:
    """Аудитория бота агрегатами, без chat_id и имён (AN-011): подписки по источнику и
    retention D7/D30 по недельным когортам. Подписка — запись подписчика (идёт) или строка
    departed (закончилась); отписанные до AN-011 не учитываются: дат у них нет."""
    today = today or library.today()
    spells = [
        {"added": s.added, "source": s.start_param, "left": None} for s in state.active() if s.added
    ]
    spells += [d for d in state.departed if d.get("added")]
    by_source: dict[str, dict] = {}
    for spell in spells:
        row = by_source.setdefault(spell.get("source") or "—", {"subscribed": 0, "active": 0})
        row["subscribed"] += 1
        row["active"] += spell.get("left") is None
    cohorts: dict[str, list[dict]] = {}
    for spell in spells:
        start = date.fromisoformat(spell["added"][:10])
        week = (start - timedelta(days=start.weekday())).isoformat()
        cohorts.setdefault(week, []).append(spell)
    rows = []
    for week in sorted(cohorts):
        group = cohorts[week]
        row = {"cohort": week, "n": len(group)}
        for days in (7, 30):
            # В знаменателе — только те, кому уже исполнилось days дней подписки.
            ripe = [g for g in group if _days(g["added"], today) >= days]
            kept = [
                g for g in ripe if g.get("left") is None or _days(g["added"], g["left"]) >= days
            ]
            row[f"d{days}"] = round(len(kept) / len(ripe), 2) if ripe else None
            row[f"d{days}_n"] = len(ripe)
        rows.append(row)
    return {
        "active": len(state.active()),
        "by_source": dict(sorted(by_source.items(), key=lambda kv: -kv[1]["subscribed"])),
        "cohorts": rows,
    }


_SERVING = False  # этот процесс держит SERVE_LOCK (`bot serve`)


def serve_lock_path(root: Path | None = None) -> Path:
    return state_path(root).with_name(SERVE_LOCK)


@contextlib.contextmanager
def serving(root: Path | None = None) -> Iterator[None]:
    """Держит `bot serve`: второй serve — TelegramError, остальные не зовут getUpdates."""
    global _SERVING
    path = serve_lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            raise TelegramError("bot serve уже запущен в другом процессе") from e
        fh.seek(0)
        fh.truncate()
        fh.write(f"{os.getpid()}\n")
        fh.flush()
        _SERVING = True
        try:
            yield
        finally:
            _SERVING = False
            fcntl.flock(fh, fcntl.LOCK_UN)


def serve_running(root: Path | None = None) -> bool:
    """Запущен ли `bot serve` в другом процессе: его flock на SERVE_LOCK не взять."""
    if _SERVING:
        return False
    path = serve_lock_path(root)
    if not path.exists():
        return False
    with open(path) as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fh, fcntl.LOCK_UN)
    return False


def recipients(
    state: State, entry: Entry | None = None, channel: str | None = None
) -> list[Subscriber]:
    """Кому уходит озвучка: канал, если он задан, иначе активные подписчики.

    channel — куда слать вместо обычного канала: имя из `telegram_tems.md`,
    id или @username. Без него статья идёт в `ARTICLE_CHANNEL`, дайджест
    новостей — в `NEWS_CHANNEL`.
    """
    chat, thread = resolve_channel(channel) if channel else _default_channel(entry)
    if chat is None:
        return state.active()
    return [Subscriber(chat_id=chat, added="", thread=thread)]


def state_path(root: Path | None = None) -> Path:
    return (root or library.library_root()) / STATE_NAME


def _multipart(params: dict, files: dict[str, Path]) -> tuple[bytes, str]:
    """Тело multipart/form-data: параметры строками, файлы — как есть."""
    boundary = "----narrator" + secrets.token_hex(16)
    sep = f"--{boundary}\r\n".encode()
    body = bytearray()
    for name, value in params.items():
        body += sep
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += f"{value}\r\n".encode()
    for name, path in files.items():
        kind = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        body += sep
        body += (
            f'Content-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
            f"Content-Type: {kind}\r\n\r\n"
        ).encode()
        body += path.read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def http_transport(token: str, api_root: str = API_ROOT) -> Callable[..., dict]:
    """Настоящий транспорт до api.telegram.org."""

    def call(method: str, params: dict, files: dict[str, Path] | None = None) -> dict:
        url = f"{api_root}/bot{token}/{method}"
        if files:
            data, content_type = _multipart(params, files)
        else:
            data = json.dumps(params).encode("utf-8")
            content_type = "application/json"
        request = urllib.request.Request(url, data=data, headers={"Content-Type": content_type})
        timeout = TIMEOUT + int(params.get("timeout", 0))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # ошибки API приходят телом с ok=false
            try:
                return json.loads(e.read().decode("utf-8"))
            except (ValueError, OSError):
                raise TelegramError(f"{method}: HTTP {e.code}") from e
        except urllib.error.URLError as e:
            # urllib заворачивает в URLError сбои соединения и отправки запроса: он не дошёл.
            raise NotSent(f"{method}: {e}") from e
        except (OSError, ValueError) as e:
            # Запрос ушёл, ответа нет или он битый (таймаут чтения, обрыв) — сообщение могло уйти.
            raise TelegramError(f"{method}: {e}") from e

    return call


@dataclass
class Reply:
    """Ответ Telegram: ok и данные либо код с описанием ошибки."""

    ok: bool
    result: object = None
    code: int = 0
    description: str = ""
    retry_after: float = 0  # 429: сколько секунд Telegram просит подождать


def backoff(attempt: int) -> float:
    """Пауза перед повтором: 1, 2, 4… с, с разбросом ±50% — повторы не бьют разом."""
    return BACKOFF_S * 2 ** (attempt - 1) * (0.5 + random.random())


class Bot:
    """Bot API с повторами и темпом отправки; sleep и clock подменяют в тестах."""

    def __init__(
        self,
        token: str,
        transport: Callable[..., dict] | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rate: float = SEND_RATE,
    ):
        self.transport = transport or http_transport(token)
        self._sleep, self._clock = sleep, clock
        self._gap = 1 / rate if rate else 0.0
        self._last_send: float | None = None

    def _pace(self) -> None:
        """Отправки не чаще rate в секунду: рассылка по растущему списку не упрётся в 429."""
        if self._last_send is not None:
            wait = self._last_send + self._gap - self._clock()
            if wait > 0:
                self._sleep(wait)
        self._last_send = self._clock()

    def request(self, method: str, params: dict | None = None, files: dict | None = None) -> Reply:
        safe = method in SAFE_METHODS
        for attempt in range(1, ATTEMPTS + 1):
            last = attempt == ATTEMPTS
            if not safe:
                self._pace()
            try:
                data = self.transport(method, params or {}, files)
            except NotSent:
                if last:
                    raise
                self._sleep(backoff(attempt))
                continue
            except TelegramError:
                if not safe or last:  # отправка без ответа могла дойти — решает человек
                    raise
                self._sleep(backoff(attempt))
                continue
            reply = _reply(data)
            if reply.ok or last:
                return reply
            if reply.code == 429 and reply.retry_after <= MAX_RETRY_AFTER_S:
                self._sleep(reply.retry_after or backoff(attempt))
                continue
            if safe and reply.code >= 500:
                self._sleep(backoff(attempt))
                continue
            return reply
        raise AssertionError("недостижимо")  # pragma: no cover

    def call(self, method: str, params: dict | None = None, files: dict | None = None) -> object:
        reply = self.request(method, params, files)
        if not reply.ok:
            raise TelegramError(f"{method}: {reply.description} (код {reply.code})")
        return reply.result


def _reply(data: dict) -> Reply:
    if data.get("ok"):
        return Reply(ok=True, result=data.get("result"))
    params = data.get("parameters") or {}
    return Reply(
        ok=False,
        code=int(data.get("error_code", 0)),
        description=str(data.get("description", "неизвестная ошибка")),
        retry_after=float(params.get("retry_after") or 0) if isinstance(params, dict) else 0,
    )


def chat_name(chat: dict) -> str:
    """Имя чата для показа человеку (`bot subscribers --names`); в файл не пишется."""
    parts = [chat.get("first_name"), chat.get("last_name")]
    name = " ".join(p for p in parts if p) or chat.get("title") or ""
    username = chat.get("username")
    if username:
        name = f"{name} @{username}".strip()
    return name or str(chat.get("id", "?"))


def _env_number(name: str, default: float) -> float:
    try:
        return max(float(os.environ.get(name, "") or default), 0.0)
    except ValueError:
        return default


class ReplyLimiter:
    """Не чаще одного ответа чату в interval секунд; память — только о недавних чатах."""

    def __init__(self, interval: float | None = None, clock: Callable[[], float] | None = None):
        if interval is None:
            interval = _env_number(REPLY_INTERVAL_ENV, REPLY_INTERVAL_S)
        self.interval = interval
        self._clock = clock or time.monotonic
        self._last: dict[int, float] = {}

    def allow(self, chat_id: int) -> bool:
        now = self._clock()
        if len(self._last) > 10_000:  # флуд с тысяч чатов не раздувает память
            self._last = {k: t for k, t in self._last.items() if now - t < self.interval}
        last = self._last.get(chat_id)
        if last is not None and now - last < self.interval:
            return False
        self._last[chat_id] = now
        return True


_LIMITER = ReplyLimiter()  # общий на процесс: bot serve зовёт pull в цикле


def pull(
    bot: Bot,
    state: State,
    timeout: int = 0,
    limiter: ReplyLimiter | None = None,
    max_subscribers: int | None = None,
    invite: str | None = None,
    on_update: Callable[[dict], None] | None = None,
) -> list[Subscriber]:
    """Забирает накопившиеся сообщения и ведёт по ним список подписчиков.

    /start — подписка, /stop — отписка с удалением записи, остальное — подсказка. Возвращает тех,
    кто подписался в этот заход. Курсор getUpdates хранится в state.offset,
    поэтому одно и то же сообщение не обрабатывается дважды.

    Флуд (SEC-012): ответ чату — не чаще раза в interval лимитера; повторный
    /start уже подписанного — без ответа; сверх max_subscribers и без
    инвайт-кода (`/start <код>`, если он задан) — не подписываем.

    on_update — кому отдать нажатия кнопок (callback_query) и ответы на сообщения
    бота: ревью выпуска платформы (PRD-001). Без него кнопки не запрашиваются.

    Пока работает `bot serve` в другом процессе, getUpdates не зовём: два опроса
    дают 409 и роняют serve. Подписки тогда забирает он.
    """
    if serve_running():
        return []
    limiter = limiter or _LIMITER
    if max_subscribers is None:
        max_subscribers = int(_env_number(MAX_SUBSCRIBERS_ENV, MAX_SUBSCRIBERS))
    if invite is None:
        invite = os.environ.get(INVITE_ENV, "").strip()

    def reply(chat_id: int, text: str) -> None:
        if limiter.allow(chat_id):
            bot.request("sendMessage", {"chat_id": chat_id, "text": text})

    kinds = ["message", "callback_query"] if on_update else ["message"]
    params = {"offset": state.offset, "allowed_updates": kinds}
    if timeout:
        params["timeout"] = timeout
    updates = bot.call("getUpdates", params) or []
    fresh: list[Subscriber] = []
    for update in updates:
        state.offset = max(state.offset, int(update["update_id"]) + 1)
        msg = update.get("message") or {}
        if on_update is not None and (update.get("callback_query") or msg.get("reply_to_message")):
            on_update(update)  # кто нажал и можно ли — решает платформа
            continue
        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            continue
        words = (msg.get("text") or "").strip().split()
        command = words[0].split("@")[0].lower() if words else ""
        if command == "/start":
            if state.find(chat_id) is not None:
                continue  # уже подписан — молчим
            code, source = start_source(words[1] if len(words) > 1 else "", invite)
            if invite and not secrets.compare_digest(code.encode(), invite.encode()):
                reply(chat_id, NEED_INVITE)
            elif max_subscribers and len(state.active()) >= max_subscribers:
                reply(chat_id, FULL)
            elif state.upsert(chat_id, start_param=source):
                fresh.append(state.find(chat_id))
                reply(chat_id, welcome())
        elif command == "/stop":
            if state.remove(chat_id):  # отзыв согласия — данные удаляем сразу (CMP-006)
                reply(chat_id, GOODBYE)
        else:
            reply(chat_id, HINT)
    return fresh


def spoken_text(path: Path, entry: Entry) -> str:
    """Всё, что прозвучит и будет видно: заголовок, summary, текст озвучки и пост."""
    parts = [entry.title, entry.summary]
    for name in ("script.txt", "narration.md", "post.md"):
        f = path / name
        if f.exists():
            parts.append(f.read_text(encoding="utf-8"))
    return "\n".join(parts)


def describe(path: Path, entry: Entry, engine: str) -> str:
    """Подпись под аудио (HTML): название, о чём статья, сколько слушать, откуда.

    Упомянута организация из реестра запрещённых — в конце сноска; режется
    только summary, сноска остаётся целиком.
    """
    duration = entry.versions.get(engine, entry.duration_sec)
    facts = [f"🎧 {format_duration(duration)}"]
    if entry.author:
        facts.append(entry.author)
    tail = " · ".join(facts)
    if entry.url:
        tail += f'\n<a href="{html.escape(entry.url)}">Оригинал статьи</a>'
    tail += f"\n{registry.ai_line(voice=True)}"  # озвучка narrator — всегда синтез (CMP-011)
    link = start_link(start_tag(path.name))
    if link:  # подписчик из этого поста помечен меткой статьи (AN-011)
        tail += f'\n<a href="{html.escape(link)}">Подписаться на озвучки</a>'
    notes = registry.footnotes_html(spoken_text(path, entry), registry.load())
    head = f"<b>{html.escape(entry.title.strip())}</b>"
    # Лимит Telegram считает текст без разметки, так что запас тут с избытком.
    room = CAPTION_LIMIT - len(head) - len(tail) - len(notes) - 6
    if room < 0:
        raise TelegramError("подпись не влезает в 1024 знака: длинные сноски реестра")
    summary = " ".join(entry.summary.split())
    if len(summary) > room:
        summary = summary[: max(room - 1, 0)].rstrip() + "…"
    return "\n\n".join(part for part in (head, html.escape(summary), tail, notes) if part)


ANNOTATION_SHARE = 0.2  # аннотация без права на переработку — не больше пятой части статьи


def no_license(path: Path, entry: Entry) -> str:
    return (
        f"{path.name}: license={entry.license} — права на переработку нет, полный пересказ"
        " в канал не уходит (слушать локально можно). Есть право — license в meta.json:"
        " own, cc-by или permission; иначе только аннотация: ./narrate bot send"
        f" {path} --annotation"
    )


def annotation_text(path: Path, entry: Entry) -> str:
    """Аннотация (HTML): заголовок, summary не длиннее 20 % статьи, автор и ссылка, сноски."""
    found = next((f for f in (path / "source.md", path / "script.txt") if f.exists()), None)
    body = found.read_text(encoding="utf-8") if found else ""
    room = int(len(" ".join(body.split())) * ANNOTATION_SHARE) if body.strip() else 400
    summary = " ".join(entry.summary.split())
    if len(summary) > room:
        summary = summary[: max(room - 1, 0)].rstrip() + "…"
    facts = " · ".join(x for x in (entry.author,) if x)
    link = f'<a href="{html.escape(entry.url)}">Оригинал статьи</a>' if entry.url else ""
    notes = registry.footnotes_html(f"{entry.title}\n{summary}", registry.load())
    head = f"<b>{html.escape(entry.title.strip())}</b>"
    parts = (head, html.escape(summary), "\n".join(p for p in (facts, link) if p), notes)
    return "\n\n".join(p for p in parts if p)


def _send_annotation(
    bot: Bot, state: State, path: Path, entry: Entry, engine: str, people, tag: str
) -> list[Delivery]:
    if not entry.url:
        raise TelegramError(f"{path.name}: у аннотации нужна ссылка на оригинал — url в meta.json")
    text = annotation_text(path, entry)
    done = []
    for sub in people:
        reply = bot.request("sendMessage", {**sub.address(), "text": text, "parse_mode": "HTML"})
        if not reply.ok and _is_gone(reply):
            state.remove(sub.chat_id)  # бот заблокирован — данные удаляем (CMP-006)
        done.append(Delivery(sub.chat_id, reply.ok, "" if reply.ok else reply.description))
    if any(d.ok for d in done):
        mark_sent(state, path, engine, tag, caption=text, done=done)
    return done


def sent_tag(entry: Entry | None, channel: str | None) -> str:
    """Хвост отметки об отправке, когда канал назвали сами: `@<id>` или `@<id>_<тема>`.

    Пусто — материал идёт в свой обычный канал. Так отправка в другой канал
    не мешает обычной и сама не повторяется дважды.
    """
    if not channel:
        return ""
    chat, thread = resolve_channel(channel)
    try:
        usual = _default_channel(entry)
    except TelegramError:  # обычный канал не записан — сравнивать не с чем
        usual = None
    if chat is None or (chat, thread) == usual:
        return ""
    return _tag(chat, thread)


def _tag(chat: int | str, thread: int | None) -> str:
    return f"@{chat}" if thread is None else f"@{chat}_{thread}"


def sent_key(path: Path, engine: str, tag: str = "") -> str:
    return f"{article_id(path)}_{engine}{tag}"


def is_fresh(state: State, path: Path, engine: str, tag: str = "") -> bool:
    """Эту озвучку ещё не рассылали или она переозвучена после рассылки.

    tag — `sent_tag()`, если шлём не в обычный канал.
    """
    stamp = state.sent.get(sent_key(path, engine, tag))
    if not stamp:
        return True
    audio = path / audio_name(path, engine)
    if not audio.exists():
        return False
    # Отметку пишем в секундах, поэтому и время файла округляем: озвучка той же
    # секунды, что и рассылка, — это она и есть.
    mtime = datetime.fromtimestamp(audio.stat().st_mtime).replace(microsecond=0)
    return mtime > datetime.fromisoformat(stamp)


def unsent(state: State, root: Path) -> list[tuple[Path, Entry, str]]:
    """Озвучки, которые ещё не ушли в телеграм, старые первыми.

    Берётся последняя озвученная версия статьи (`engine` в meta.json): её mp3
    есть, а отметки о рассылке нет или mp3 новее отметки.
    """
    out = []
    for path, entry in reversed(library.entries(root)):
        engine = entry.engine or (sorted(entry.versions) or [""])[0]
        if not engine or not (path / audio_name(path, engine)).exists():
            continue
        if is_fresh(state, path, engine):
            out.append((path, entry, engine))
    return out


def mark_sent(
    state: State,
    path: Path,
    engine: str,
    tag: str = "",
    *,
    caption: str = "",
    media: tuple[Path, ...] = (),
    done: list[Delivery] | None = None,
) -> None:
    """Отметить озвучку отправленной и записать это в журнал комплаенса (CMP-015).

    Без done — «считать отправленной, ничего не посылая» (`bot send --mark`)."""
    state.sent[sent_key(path, engine, tag)] = datetime.now().isoformat(timespec="seconds")
    audit.write(
        library.library_root(),
        "publish" if done is not None else "mark_sent",
        item=article_id(path) if path.parent.parent.parent.name.isdigit() else str(path),
        engine=engine,
        channel=tag or None,
        recipients=[d.chat_id for d in done or [] if d.ok] or None,
        caption_sha256=audit.sha256_text(caption) if caption else None,
        media={p.name: audit.sha256_file(p) for p in media if p} or None,
        footnotes=audit.footnotes(caption) or None,
        registry=registry.checked(),
    )


@dataclass
class Delivery:
    chat_id: int
    ok: bool
    error: str = ""


_GONE = ("blocked", "deactivated", "chat not found", "kicked", "user is deactivated")


def _is_gone(reply: Reply) -> bool:
    low = reply.description.lower()
    return reply.code in (400, 403) and any(sign in low for sign in _GONE)


def broadcast(
    bot: Bot,
    state: State,
    path: Path,
    entry: Entry,
    engine: str,
    targets: list[Subscriber] | None = None,
    channel: str | None = None,
    annotation: bool = False,
) -> list[Delivery]:
    """Шлёт озвучку статьи в канал или подписчикам (см. `recipients`).

    Полный пересказ чужой статьи уходит, только если в meta.json есть право
    на переработку (`license` из PUBLISHABLE); иначе TelegramError.
    annotation — вместо озвучки аннотация (до 20 % объёма) со ссылкой на оригинал.

    channel — другой канал вместо обычного. Заблокировавших бота подписчиков
    удаляет из списка (CMP-006).

    Файл больше 50 МБ Telegram от бота не примет — тогда уходит только описание
    с пометкой, где лежит mp3.
    """
    audio = path / audio_name(path, engine)
    if not audio.exists():
        raise TelegramError(f"{audio}: нет файла — сначала narrator synth")
    people = recipients(state, entry, channel) if targets is None else targets
    if not people:
        return []
    tag = sent_tag(entry, channel)
    if entry.kind == "digest":
        from narrator.news.cover import CoverError, find_cover, thumbnail

        try:  # без картинки дайджест не уходит
            cover = find_cover(path)
            thumb = thumbnail(cover)
        except (CoverError, OSError) as e:
            raise TelegramError(f"обложки нет: {e}") from e
        done = [
            _send_digest(bot, state, path, entry, engine, audio, (cover, thumb), sub)
            for sub in people
        ]
        if any(d.ok for d in done):
            caption = digest_caption(path, entry)
            mark_sent(state, path, engine, tag, caption=caption, media=(cover, audio), done=done)
        return done

    if entry.kind == "article" and annotation:
        return _send_annotation(bot, state, path, entry, engine, people, tag)
    if entry.kind == "article" and entry.license not in library.PUBLISHABLE:
        raise TelegramError(no_license(path, entry))
    caption = describe(path, entry, engine)
    duration = int(entry.versions.get(engine, entry.duration_sec) or 0)
    oversized = audio.stat().st_size > SIZE_LIMIT
    done: list[Delivery] = []
    for sub in people:
        if oversized:
            note = (
                f"{caption}\n\nФайл больше 50 МБ — Telegram такие от бота не берёт. "
                f"Аудио: {audio.name}"
            )
            reply = bot.request(
                "sendMessage", {**sub.address(), "text": note, "parse_mode": "HTML"}
            )
        else:
            reply = bot.request(
                "sendAudio",
                {
                    **sub.address(),
                    "caption": caption,
                    "parse_mode": "HTML",
                    "title": entry.title,
                    "performer": entry.author or "narrator",
                    "duration": duration,
                },
                {"audio": audio},
            )
        if not reply.ok and _is_gone(reply):
            state.remove(sub.chat_id)  # бот заблокирован — данные удаляем (CMP-006)
        error = "" if reply.ok else reply.description
        done.append(Delivery(sub.chat_id, reply.ok, error))
    if any(d.ok for d in done):
        mark_sent(state, path, engine, tag, caption=caption, media=(audio,), done=done)
    return done


# Дайджест: озвучка — синтез (Gemini TTS или локальная модель), картинка — ChatGPT.
DIGEST_AI = registry.ai_line(voice=True, images=True)


def digest_caption(path: Path, entry: Entry) -> str:
    """Подпись к картинке дайджеста: пост из post.md со ссылками на источники.

    Сноски о запрещённых организациях из пересказа и поста дописываются в
    конец; длинный пост режется по предложению, сноски — никогда.
    """
    post = path / "post.md"
    text = post.read_text(encoding="utf-8") if post.exists() else ""
    if not text.strip():
        text = f"**{entry.title.replace('*', '')}**\n\n{entry.summary.strip()}"
    entries = registry.load()
    text = registry.with_footnotes(text, spoken_text(path, entry), entries)
    text = registry.with_materials(text, registry.materials(digest_agents(path), entries))
    text = registry.with_ai_line(text, DIGEST_AI)
    try:
        return registry.fit_caption(text, CAPTION_LIMIT)
    except registry.CaptionError as e:
        raise TelegramError(str(e)) from e


def _send_digest(
    bot: Bot,
    state: State,
    path: Path,
    entry: Entry,
    engine: str,
    audio: Path,
    images: tuple[Path, Path],
    sub: Subscriber,
) -> Delivery:
    """Дайджест одному получателю — двумя сообщениями: картинка с постом, затем аудио.

    images — картинка и её миниатюра для обложки аудио.
    """
    cover, thumb = images
    day = entry.title.split(", ", 1)[-1]  # «22 сентября»
    params = {**sub.address(), "parse_mode": "HTML"}
    reply = bot.request(
        "sendPhoto", {**params, "caption": digest_caption(path, entry)}, {"photo": cover}
    )
    if reply.ok:
        if audio.stat().st_size > SIZE_LIMIT:
            note = f"Аудио больше 50 МБ — Telegram такие от бота не берёт: {audio.name}"
            reply = bot.request("sendMessage", {**params, "text": html.escape(note)})
        else:
            reply = bot.request(
                "sendAudio",
                {
                    **params,
                    "caption": audio_caption(path, entry, f"Новости за {day}"),
                    "title": day,
                    "performer": "Дайджест разработчика",
                    "duration": int(entry.versions.get(engine, entry.duration_sec) or 0),
                },
                {"audio": audio, "thumbnail": thumb},
            )
    if not reply.ok and _is_gone(reply):
        state.remove(sub.chat_id)  # бот заблокирован — данные удаляем (CMP-006)
    return Delivery(sub.chat_id, reply.ok, "" if reply.ok else reply.description)


def audio_caption(path: Path, entry: Entry, title: str) -> str:
    """Подпись к аудио дайджеста: аудио могут переслать отдельно — сноски и маркировка тут же."""
    entries = registry.load()
    notes = registry.footnotes_html(spoken_text(path, entry), entries)
    labels = "\n".join(html.escape(m) for m in registry.materials(digest_agents(path), entries))
    ai = html.escape(DIGEST_AI)
    return "\n\n".join(p for p in (labels, html.escape(title), notes, ai) if p)


def digest_agents(path: Path) -> list[str]:
    """Иноагенты, чьи материалы вошли в дайджест path (отметка в news.db при сборе, CMP-012)."""
    from narrator.news.store import db_path, issue_agents

    return issue_agents(db_path(library.library_root()), article_id(path))


def send_digest_video(
    bot: Bot,
    state: State,
    path: Path,
    entry: Entry,
    engine: str,
    video: Path,
    covers: tuple[Path, Path],
    channel: str | None = None,
) -> list[Delivery]:
    """Дайджест одним постом — видео со статичной картинкой, подпись — post.md.

    channel — другой канал вместо канала новостей.

    covers — обложка видео (плей и плашка «подпишись») и её миниатюра: по ним
    в ленте видно, что это видео, а не картинка.

    Отметка об отправке та же, что у поста из картинки и аудио: выпуск уходит
    в канал один раз, в каком бы виде ни ушёл. engine — озвучка, из которой видео.
    """
    from narrator.news.video import video_size

    if video.stat().st_size > SIZE_LIMIT:
        raise TelegramError(f"{video.name} больше 50 МБ — Telegram такие от бота не берёт")
    width, height = video_size(video)
    cover, thumb = covers
    done = []
    for sub in recipients(state, entry, channel):
        reply = bot.request(
            "sendVideo",
            {
                **sub.address(),
                "caption": digest_caption(path, entry),
                "parse_mode": "HTML",
                "duration": int(entry.duration_sec or 0),
                "width": width,
                "height": height,
                "supports_streaming": "true",  # multipart шлёт строки
                "cover": "attach://cover",
            },
            {"video": video, "thumbnail": thumb, "cover": cover},
        )
        if not reply.ok and _is_gone(reply):
            state.remove(sub.chat_id)  # бот заблокирован — данные удаляем (CMP-006)
        error = "" if reply.ok else reply.description
        done.append(Delivery(sub.chat_id, reply.ok, error))
    if any(d.ok for d in done):
        tag = sent_tag(entry, channel)
        mark_sent(
            state, path, engine, tag, caption=digest_caption(path, entry), media=(video,), done=done
        )
    return done


# --- папка без meta.json -------------------------------------------------

FOLDER_ENGINE = "folder"  # «движок» в отметке об отправке такой папки
MESSAGE_LIMIT = 4096


@dataclass
class Parcel:
    """Что нашлось в папке без meta.json: самые свежие mp3, картинка и текст поста."""

    audio: Path | None = None
    image: Path | None = None
    post: Path | None = None

    def text(self) -> str:
        return self.post.read_text(encoding="utf-8").strip() if self.post else ""

    def title(self) -> str:
        """Первая строка поста без разметки, иначе имя mp3."""
        first = next((line for line in self.text().splitlines() if line.strip()), "")
        first = first.strip().strip("#*_ ").strip()
        return first or (self.audio.stem if self.audio else "")


def find_parcel(path: Path) -> Parcel:
    """Материал в свободной папке: mp3, картинка и `post.md` или `<имя>-post.md`.

    Файлов одного вида несколько — берётся самый свежий. Ничего не
    переименовывает: папка чужая.
    """
    from narrator.news.cover import IMAGE_EXT
    from narrator.news.video import FRAMES

    def newest(files) -> Path | None:
        return max(files, key=lambda p: p.stat().st_mtime, default=None)

    images = (p for p in path.iterdir() if p.suffix.lower() in IMAGE_EXT and p.name not in FRAMES)
    return Parcel(  # кадры видео, собранные в прошлый раз, — не картинка
        audio=newest(path.glob("*.mp3")),
        image=newest(images),
        post=newest(path.glob("*post.md")),
    )


def folder_tag(channel: str) -> str:
    """Хвост отметки об отправке папки: обычного канала у неё нет, канал называют всегда."""
    chat, thread = resolve_channel(channel)
    if chat is None:
        raise TelegramError(f"папку шлют в канал: назови его именем из {CHANNELS_FILE}")
    return _tag(chat, thread)


def send_folder(
    bot: Bot,
    state: State,
    path: Path,
    channel: str,
    duration: float = 0,
    video: Path | None = None,
    covers: tuple[Path, Path] | None = None,
) -> list[Delivery]:
    """Шлёт в канал папку без meta.json — как дайджест новостей.

    video и covers (обложка видео и её миниатюра) — один пост: видео с текстом
    поста в подписи. Без них — картинка с текстом поста, затем аудио; картинки
    нет — текст идёт подписью к аудио, нет и аудио — сообщением. Текст поста —
    как `post.md` дайджеста: `**жирный**` и `[текст](url)`.
    """
    from narrator.news.digest import post_html, visible_length
    from narrator.news.video import video_size

    parcel = find_parcel(path)
    # Сноски о запрещённых организациях — в конец поста, как у дайджеста.
    text = registry.with_footnotes(parcel.text(), parcel.text(), registry.load())
    caption = post_html(text) if parcel.post else ""
    if not (parcel.audio or parcel.image or caption):
        raise TelegramError(f"в {path} нечего слать: нужен mp3, картинка или *post.md")
    limit = CAPTION_LIMIT if parcel.audio or parcel.image else MESSAGE_LIMIT
    if visible_length(caption) > limit:
        raise TelegramError(
            f"{parcel.post.name}: {visible_length(caption)} знаков"
            f"{' вместе со сносками реестра' if text != parcel.text() else ''},"
            f" Telegram берёт до {limit}"
        )
    if parcel.audio and parcel.audio.stat().st_size > SIZE_LIMIT:
        raise TelegramError(f"{parcel.audio.name} больше 50 МБ — Telegram такие от бота не берёт")
    if video is not None and video.stat().st_size > SIZE_LIMIT:
        raise TelegramError(f"{video.name} больше 50 МБ — Telegram такие от бота не берёт")
    tag = folder_tag(channel)
    done = []
    for sub in recipients(state, None, channel):
        params = {**sub.address(), "parse_mode": "HTML"}
        steps = []
        if video is not None:
            width, height = video_size(video)
            cover, thumb = covers
            clip = {
                **params,
                "caption": caption,
                "duration": int(duration or 0),
                "width": width,
                "height": height,
                "supports_streaming": "true",  # multipart шлёт строки
                "cover": "attach://cover",
            }
            steps.append(("sendVideo", clip, {"video": video, "thumbnail": thumb, "cover": cover}))
        elif parcel.image:
            steps.append(("sendPhoto", {**params, "caption": caption}, {"photo": parcel.image}))
        elif caption and not parcel.audio:
            steps.append(("sendMessage", {**params, "text": caption}, None))
        if parcel.audio and video is None:
            audio = {**params, "title": parcel.title(), "duration": int(duration or 0)}
            if not parcel.image:
                audio["caption"] = caption
            steps.append(("sendAudio", audio, {"audio": parcel.audio}))
        reply = Reply(ok=True)
        for method, body, files in steps:
            reply = bot.request(method, body, files)
            if not reply.ok:
                break
        error = "" if reply.ok else reply.description
        done.append(Delivery(sub.chat_id, reply.ok, error))
    if any(d.ok for d in done):
        files = tuple(p for p in (parcel.image, parcel.audio, video) if p)
        mark_sent(state, path, FOLDER_ENGINE, tag, caption=caption, media=files, done=done)
    return done
