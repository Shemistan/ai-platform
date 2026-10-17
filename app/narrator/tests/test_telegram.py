import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from narrator import telegram
from narrator.library import Entry
from narrator.telegram import Bot, State


@pytest.fixture(autouse=True)
def no_channel(monkeypatch):
    """По умолчанию тесты шлют подписчикам в личку; канал включают явно."""
    monkeypatch.setenv(telegram.CHANNEL_ENV, "off")


class FakeTransport:
    """Вместо сети: копит вызовы и отдаёт заранее заданные ответы."""

    def __init__(self, replies: dict | None = None):
        self.calls: list[tuple[str, dict, list[str]]] = []
        self.replies = replies or {}

    def __call__(self, method: str, params: dict, files: dict | None = None) -> dict:
        self.calls.append((method, params, sorted(files or {})))
        default = {"ok": True, "result": [] if method == "getUpdates" else {"message_id": 1}}
        reply = self.replies.get(method, default)
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else {"ok": True, "result": []}
        return reply

    def methods(self) -> list[str]:
        return [c[0] for c in self.calls]

    def sent_to(self, method: str) -> list[int]:
        return [c[1]["chat_id"] for c in self.calls if c[0] == method]


def message(update_id: int, chat_id: int, text: str, name: str = "Аня") -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "type": "private", "first_name": name}, "text": text},
    }


def article(tmp_path: Path, engine: str = "silero", size: int = 2048) -> tuple[Path, Entry]:
    path = tmp_path / "2026" / "09" / "18" / "triple-debt"
    path.mkdir(parents=True)
    entry = Entry(
        title="Тройной долг",
        added="2026-09-18",
        url="https://x.test/a",
        summary="Технический долг растёт втрое быстрее, чем кажется.",
        author="Аноним",
        voice="eugene",
        engine=engine,
        duration_sec=125.0,
        versions={engine: 125.0},
        license="permission",  # право на переработку есть — полный пересказ уходит в канал
    )
    entry.save(path / "meta.json")
    (path / f"2026-09-18-triple-debt_{engine}.mp3").write_bytes(b"\0" * size)
    return path, entry


# --- токен ---------------------------------------------------------------

FAKE_TOKEN = "123456:" + "A" * 35


def test_read_token_from_file_ignoring_markdown(tmp_path, monkeypatch):
    monkeypatch.delenv(telegram.TOKEN_ENV, raising=False)
    path = tmp_path / "bot_token.md"
    # Синтетический токен: настоящий в тестах держать нельзя (SEC-001).
    path.write_text(f"# токен бота\n\n`{FAKE_TOKEN}`\n")
    monkeypatch.setenv(telegram.TOKEN_FILE_ENV, str(path))
    assert telegram.read_token() == FAKE_TOKEN


def test_read_token_prefers_env(tmp_path, monkeypatch):
    monkeypatch.setenv(telegram.TOKEN_ENV, "111:AAAA")
    monkeypatch.setenv(telegram.TOKEN_FILE_ENV, str(tmp_path / "нет.md"))
    assert telegram.read_token() == "111:AAAA"


def test_read_token_without_source_raises(tmp_path, monkeypatch):
    monkeypatch.delenv(telegram.TOKEN_ENV, raising=False)
    monkeypatch.setenv(telegram.TOKEN_FILE_ENV, str(tmp_path / "нет.md"))
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path / "library"))
    with pytest.raises(telegram.TelegramError, match="токен"):
        telegram.read_token()


# --- состояние -----------------------------------------------------------


def test_state_roundtrip_and_unknown_fields(tmp_path):
    state = State(offset=7)
    state.upsert(42, "Аня")
    state.sent["2026-09-18-triple-debt_silero"] = "2026-09-18T10:00:00"
    path = tmp_path / "telegram.json"
    state.save(path)
    assert State.load(path) == state
    assert State.load(tmp_path / "нет.json") == State()


def test_state_save_is_owner_only(tmp_path):
    # Подписчики — личные данные: файл 0600 и после перезаписи, временных не остаётся (SEC-010).
    path = tmp_path / "telegram.json"
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o644)
    State(offset=1).save(path)
    assert path.stat().st_mode & 0o777 == 0o600
    assert not [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_upsert_returns_true_only_for_new_chat():
    state = State()
    assert state.upsert(42, "Аня") is True
    assert state.upsert(42, "Аня") is False
    assert len(state.subscribers) == 1


def test_upsert_revives_unsubscribed():
    state = State()
    state.upsert(42, "Аня")
    state.deactivate(42, "отписался")
    assert state.active() == []
    assert state.upsert(42, "Аня") is True
    assert [s.chat_id for s in state.active()] == [42]


# --- приём подписчиков ---------------------------------------------------


def test_pull_registers_start_advances_offset_and_greets():
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": [message(10, 42, "/start")]}]})
    state = State()
    new = telegram.pull(Bot("t", transport), state)
    assert [s.chat_id for s in new] == [42]
    assert state.subscribers[0].name == "Аня"
    assert state.offset == 11
    assert transport.sent_to("sendMessage") == [42]


def test_pull_stop_deactivates_but_keeps_record():
    updates = [message(1, 42, "/start"), message(2, 42, "/stop")]
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": updates}]})
    state = State()
    telegram.pull(Bot("t", transport), state)
    assert state.active() == []
    assert state.subscribers[0].active is False


def test_pull_ignores_repeated_start():
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": [message(1, 42, "/start")]}]})
    state = State()
    state.upsert(42, "Аня")
    assert telegram.pull(Bot("t", transport), state) == []


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    """Общий лимитер ответов процесса — свой на каждый тест."""
    monkeypatch.setattr(telegram, "_LIMITER", telegram.ReplyLimiter(interval=60))
    for name in (telegram.INVITE_ENV, telegram.MAX_SUBSCRIBERS_ENV, telegram.REPLY_INTERVAL_ENV):
        monkeypatch.delenv(name, raising=False)


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_pull_flood_gets_one_hint():
    # SEC-012: 10 сообщений подряд от одного чата — не больше одного ответа.
    updates = [message(i, 42, f"спам {i}") for i in range(1, 11)]
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": updates}]})
    clock = FakeClock()
    limiter = telegram.ReplyLimiter(interval=60, clock=clock)
    telegram.pull(Bot("t", transport), State(), limiter=limiter)
    assert transport.sent_to("sendMessage") == [42]
    clock.now += 61  # через минуту снова можно ответить
    transport.replies["getUpdates"] = [{"ok": True, "result": [message(11, 42, "ещё")]}]
    telegram.pull(Bot("t", transport), State(offset=11), limiter=limiter)
    assert transport.sent_to("sendMessage") == [42, 42]


def test_pull_repeated_start_is_silent():
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": [message(1, 42, "/start")]}]})
    state = State()
    state.upsert(42, "Аня")
    telegram.pull(Bot("t", transport), state)
    assert transport.sent_to("sendMessage") == []


def test_pull_respects_subscriber_cap_and_invite():
    updates = [message(1, 1, "/start"), message(2, 2, "/start"), message(3, 3, "/start")]
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": updates}]})
    state = State()
    telegram.pull(Bot("t", transport), state, max_subscribers=2)
    assert [s.chat_id for s in state.active()] == [1, 2]

    updates = [
        message(4, 7, "/start"),
        message(5, 8, "/start неверный"),
        message(6, 9, "/start Код-1"),
    ]
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": updates}]})
    state = State()
    new = telegram.pull(Bot("t", transport), state, invite="Код-1")
    assert [s.chat_id for s in new] == [9]
    to_7 = [p["text"] for _, p, _ in transport.calls if p.get("chat_id") == 7]
    assert to_7 == [telegram.NEED_INVITE]


def test_call_raises_on_api_error():
    error = {"ok": False, "error_code": 401, "description": "Unauthorized"}
    transport = FakeTransport({"getMe": error})
    with pytest.raises(telegram.TelegramError, match="Unauthorized"):
        Bot("t", transport).call("getMe")


# --- описание ------------------------------------------------------------


def test_describe_has_title_summary_duration_and_link(tmp_path):
    path, entry = article(tmp_path)
    text = telegram.describe(path, entry, "silero")
    assert "Тройной долг" in text
    assert "Технический долг растёт" in text
    assert "2:05" in text
    assert "https://x.test/a" in text
    assert len(text) <= telegram.CAPTION_LIMIT


def test_describe_is_html_without_tech_details(tmp_path):
    path, entry = article(tmp_path)
    entry.title = "Долг <и> проценты"
    text = telegram.describe(path, entry, "silero")
    assert text.startswith("<b>Долг &lt;и&gt; проценты</b>")
    assert '<a href="https://x.test/a">' in text
    assert "silero" not in text and "eugene" not in text


def test_describe_truncates_long_summary(tmp_path):
    path, entry = article(tmp_path)
    entry.summary = "а" * 3000
    text = telegram.describe(path, entry, "silero")
    assert len(text) <= telegram.CAPTION_LIMIT
    assert "https://x.test/a" in text


# --- рассылка ------------------------------------------------------------


def test_broadcast_sends_audio_to_active_only(tmp_path):
    path, entry = article(tmp_path)
    transport = FakeTransport()
    state = State()
    state.upsert(42, "Аня")
    state.upsert(43, "Боря")
    state.deactivate(43, "отписался")
    done = telegram.broadcast(Bot("t", transport), state, path, entry, "silero")
    assert [d.chat_id for d in done] == [42]
    assert all(d.ok for d in done)
    assert transport.sent_to("sendAudio") == [42]
    method, params, files = transport.calls[0]
    assert files == ["audio"]
    assert "Тройной долг" in params["caption"]
    assert params["duration"] == 125
    assert "2026-09-18-triple-debt_silero" in state.sent


def test_broadcast_deactivates_blocked_subscriber(tmp_path):
    path, entry = article(tmp_path)
    blocked = {"ok": False, "error_code": 403, "description": "bot was blocked by the user"}
    transport = FakeTransport({"sendAudio": [blocked]})
    state = State()
    state.upsert(42, "Аня")
    done = telegram.broadcast(Bot("t", transport), state, path, entry, "silero")
    assert done[0].ok is False
    assert state.active() == []
    assert state.subscribers[0].note == "bot was blocked by the user"


def test_broadcast_sends_text_when_file_too_big(tmp_path):
    path, entry = article(tmp_path, size=telegram.SIZE_LIMIT + 1)
    transport = FakeTransport()
    state = State()
    state.upsert(42, "Аня")
    done = telegram.broadcast(Bot("t", transport), state, path, entry, "silero")
    assert transport.methods() == ["sendMessage"]
    assert done[0].ok is True
    assert "50" in transport.calls[0][1]["text"]


def test_broadcast_without_audio_raises(tmp_path):
    path, entry = article(tmp_path)
    (path / "2026-09-18-triple-debt_silero.mp3").unlink()
    state = State()
    state.upsert(42, "Аня")
    with pytest.raises(telegram.TelegramError, match="нет файла"):
        telegram.broadcast(Bot("t", FakeTransport()), state, path, entry, "silero")


def test_broadcast_without_subscribers_returns_empty(tmp_path):
    path, entry = article(tmp_path)
    transport = FakeTransport()
    assert telegram.broadcast(Bot("t", transport), State(), path, entry, "silero") == []
    assert transport.calls == []


CHANNELS = """# каналы и темы бота
VOICE_NEWS=-1003755616451_3
HISTORY-CHAN=-1002629071990

AUDIO_ARTICLE = -1003755616451_14
"""


def channels_file(root: Path, monkeypatch, text: str = CHANNELS) -> Path:
    """telegram_tems.md в корне репозитория; библиотека — рядом, в library/."""
    monkeypatch.setenv("NARRATOR_LIBRARY", str(root / "library"))
    path = root / telegram.CHANNELS_FILE
    path.write_text(text, encoding="utf-8")
    return path


def test_channel_from_file_env_and_off(tmp_path, monkeypatch):
    monkeypatch.delenv(telegram.CHANNEL_ENV)
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path / "library"))
    with pytest.raises(telegram.TelegramError, match="telegram_tems.md"):
        telegram.channel()
    channels_file(tmp_path, monkeypatch)
    assert telegram.channel() == -1002629071990
    monkeypatch.setenv(telegram.CHANNEL_ENV, "-1001")
    assert telegram.channel() == -1001
    monkeypatch.setenv(telegram.CHANNEL_ENV, "@narrator")
    assert telegram.channel() == "@narrator"
    monkeypatch.setenv(telegram.CHANNEL_ENV, "audio_article")  # имя из файла
    assert telegram.recipients(State())[0].address() == {
        "chat_id": -1003755616451,
        "message_thread_id": 14,
    }
    monkeypatch.setenv(telegram.CHANNEL_ENV, "off")
    assert telegram.channel() is None


def test_channels_file_lists_every_channel_by_name(tmp_path, monkeypatch):
    channels_file(tmp_path, monkeypatch)
    assert telegram.channels() == {
        "VOICE_NEWS": "-1003755616451_3",
        "HISTORY_CHAN": "-1002629071990",
        "AUDIO_ARTICLE": "-1003755616451_14",
    }
    # имя — без оглядки на регистр и на дефис с подчёркиванием
    assert telegram.resolve_channel("history-chan") == (-1002629071990, None)
    assert telegram.resolve_channel("Audio_Article") == (-1003755616451, 14)
    assert telegram.resolve_channel("-1001234567890_5") == (-1001234567890, 5)
    with pytest.raises(telegram.TelegramError, match="VOICE_NEWS, HISTORY_CHAN, AUDIO_ARTICLE"):
        telegram.resolve_channel("podcast")
    with pytest.raises(telegram.TelegramError, match="ждём имя"):
        telegram.resolve_channel("--1003755616451_14")


@pytest.mark.parametrize(
    "text, error",
    [
        ("AUDIO_ARTICLE=--1003755616451_14\n", "telegram_tems.md:1"),  # опечатка в id
        ("VOICE_NEWS=-1003755616451_3\n-1002629071990\n", "telegram_tems.md:2"),  # без имени
        ("A=-1002629071990\na=-1003755616451_3\n", "записан дважды"),
    ],
)
def test_channels_file_with_a_broken_line_is_an_error(tmp_path, monkeypatch, text, error):
    channels_file(tmp_path, monkeypatch, text)
    with pytest.raises(telegram.TelegramError, match=error):
        telegram.channels()


def test_broadcast_to_a_named_channel_keeps_its_own_sent_mark(tmp_path, monkeypatch):
    """Статья, названная в другой канал, уходит туда и не мешает обычной рассылке."""
    monkeypatch.delenv(telegram.CHANNEL_ENV)
    channels_file(tmp_path, monkeypatch)
    path, entry = article(tmp_path)
    transport = FakeTransport()
    state = State()
    bot = Bot("t", transport)
    telegram.broadcast(bot, state, path, entry, "silero", channel="AUDIO_ARTICLE")
    assert transport.calls[0][1]["message_thread_id"] == 14
    tag = telegram.sent_tag(entry, "AUDIO_ARTICLE")
    assert tag == "@-1003755616451_14"
    assert not telegram.is_fresh(state, path, "silero", tag)
    assert telegram.is_fresh(state, path, "silero")  # в обычный канал ещё не уходила

    # обычный канал, названный по имени, — та же отметка, что и без имени
    assert telegram.sent_tag(entry, "history-chan") == ""
    telegram.broadcast(bot, state, path, entry, "silero", channel="history-chan")
    assert transport.sent_to("sendAudio") == [-1003755616451, -1002629071990]
    assert not telegram.is_fresh(state, path, "silero")


def test_channel_with_topic_sends_into_the_topic(tmp_path, monkeypatch):
    """«<id группы>_<id темы>», как в адресе web.telegram.org, — пост идёт в тему."""
    monkeypatch.delenv(telegram.CHANNEL_ENV)
    path, entry = article(tmp_path)
    channels_file(tmp_path, monkeypatch, "HISTORY_CHAN=-1003755616451_3\n")
    assert telegram.channel() == -1003755616451
    transport = FakeTransport()
    state = State()
    done = telegram.broadcast(Bot("t", transport), state, path, entry, "silero")
    assert [d.ok for d in done] == [True]
    params = transport.calls[0][1]
    assert params["chat_id"] == -1003755616451 and params["message_thread_id"] == 3

    monkeypatch.setenv(telegram.CHANNEL_ENV, "-1003755616451_7")
    assert telegram.recipients(state)[0].thread == 7
    # General — тема 1: Telegram ждёт сообщение без message_thread_id
    monkeypatch.setenv(telegram.CHANNEL_ENV, "-1003755616451_1")
    transport.calls.clear()
    telegram.broadcast(Bot("t", transport), state, path, entry, "silero")
    assert "message_thread_id" not in transport.calls[0][1]


def test_broadcast_goes_to_channel_not_subscribers(tmp_path, monkeypatch):
    monkeypatch.setenv(telegram.CHANNEL_ENV, "-1002629071990")
    path, entry = article(tmp_path)
    transport = FakeTransport()
    state = State()
    state.upsert(42, "Аня")
    done = telegram.broadcast(Bot("t", transport), state, path, entry, "silero")
    assert transport.sent_to("sendAudio") == [-1002629071990]
    assert done[0].ok and done[0].name == "канал"
    assert transport.calls[0][1]["parse_mode"] == "HTML"
    assert "2026-09-18-triple-debt_silero" in state.sent


def test_broadcast_to_channel_failure_keeps_subscribers(tmp_path, monkeypatch):
    monkeypatch.setenv(telegram.CHANNEL_ENV, "-1002629071990")
    path, entry = article(tmp_path)
    kicked = {"ok": False, "error_code": 403, "description": "bot was kicked from the channel"}
    state = State()
    state.upsert(42, "Аня")
    bot = Bot("t", FakeTransport({"sendAudio": [kicked]}))
    done = telegram.broadcast(bot, state, path, entry, "silero")
    assert done[0].ok is False
    assert [s.chat_id for s in state.active()] == [42]
    assert state.sent == {}


# --- настоящий HTTP ------------------------------------------------------


class FakeAPI(BaseHTTPRequestHandler):
    """Телеграм понарошку: запоминает последний запрос и отвечает ok."""

    seen: dict = {}

    def do_POST(self):  # noqa: N802 — имя задаёт BaseHTTPRequestHandler
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeAPI.seen = {"path": self.path, "type": self.headers["Content-Type"], "body": body}
        answer = json.dumps({"ok": True, "result": {"message_id": 7}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(answer)))
        self.end_headers()
        self.wfile.write(answer)

    def log_message(self, *args):
        pass


@pytest.fixture
def fake_api():
    server = HTTPServer(("127.0.0.1", 0), FakeAPI)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()


def test_http_transport_uploads_file_as_multipart(tmp_path, fake_api):
    path, entry = article(tmp_path)
    root = f"http://127.0.0.1:{fake_api.server_address[1]}"
    bot = Bot("111:AAAA", telegram.http_transport("111:AAAA", root))
    state = State()
    state.upsert(42, "Аня")
    done = telegram.broadcast(bot, state, path, entry, "silero")

    assert [d.ok for d in done] == [True]
    assert FakeAPI.seen["path"] == "/bot111:AAAA/sendAudio"
    assert FakeAPI.seen["type"].startswith("multipart/form-data; boundary=")
    body = FakeAPI.seen["body"]
    assert b'name="chat_id"' in body and b"42" in body
    assert b'name="audio"; filename="2026-09-18-triple-debt_silero.mp3"' in body
    assert b"Content-Type: audio/mpeg" in body
    assert "Тройной долг".encode() in body


def test_http_transport_sends_json_without_files(tmp_path, fake_api):
    root = f"http://127.0.0.1:{fake_api.server_address[1]}"
    bot = Bot("111:AAAA", telegram.http_transport("111:AAAA", root))
    assert bot.call("sendMessage", {"chat_id": 42, "text": "привет"}) == {"message_id": 7}
    assert FakeAPI.seen["type"] == "application/json"
    assert json.loads(FakeAPI.seen["body"]) == {"chat_id": 42, "text": "привет"}


def test_http_transport_wraps_network_failure():
    call = telegram.http_transport("111:AAAA", "http://127.0.0.1:1")
    with pytest.raises(telegram.TelegramError, match="getMe"):
        call("getMe", {})


# --- отметки об отправке -------------------------------------------------


def test_is_fresh_until_sent_and_again_after_resynth(tmp_path):
    path, _ = article(tmp_path)
    state = State()
    assert telegram.is_fresh(state, path, "silero") is True
    state.sent["2026-09-18-triple-debt_silero"] = "2100-01-01T00:00:00"
    assert telegram.is_fresh(state, path, "silero") is False
    state.sent["2026-09-18-triple-debt_silero"] = "2000-01-01T00:00:00"
    assert telegram.is_fresh(state, path, "silero") is True


# --- команды narrate bot -------------------------------------------------


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    """Библиотека во временной папке, токен из окружения, сеть — фейк."""
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path))
    monkeypatch.setenv(telegram.TOKEN_ENV, "111:AAAA")
    transport = FakeTransport()
    monkeypatch.setattr(telegram, "http_transport", lambda *a, **k: transport)
    return transport


def test_cli_subscribers_syncs_and_lists(tmp_path, bot_env, capsys):
    from narrator.cli import main

    bot_env.replies["getUpdates"] = [{"ok": True, "result": [message(5, 42, "/start", "Аня")]}]
    assert main(["bot", "subscribers"]) == 0
    out = capsys.readouterr().out
    assert "Аня" in out and "42" in out
    saved = State.load(telegram.state_path(tmp_path))
    assert [s.chat_id for s in saved.active()] == [42]
    assert saved.offset == 6


def test_cli_send_delivers_and_then_skips_until_force(tmp_path, bot_env, capsys):
    from narrator.cli import main

    article(tmp_path)
    assert main(["bot", "send", "2026-09-18/triple-debt"]) == 0
    assert "подписчиков нет" in capsys.readouterr().out

    state = State.load(telegram.state_path(tmp_path))
    state.upsert(42, "Аня")
    state.save(telegram.state_path(tmp_path))

    assert main(["bot", "send", "2026-09-18/triple-debt"]) == 0
    assert bot_env.sent_to("sendAudio") == [42]
    assert "отправлена 1 из 1" in capsys.readouterr().out

    assert main(["bot", "send", "2026-09-18/triple-debt"]) == 0
    assert bot_env.sent_to("sendAudio") == [42]
    assert "уже отправлена" in capsys.readouterr().out

    assert main(["bot", "send", "2026-09-18/triple-debt", "--force"]) == 0
    assert bot_env.sent_to("sendAudio") == [42, 42]


POST = "**Аудиокнига: паттерны MCP**\n\n🎧 Пересказ статьи.\n\n[Статья](https://arxiv.org/abs/1)\n"


def test_cli_send_a_folder_without_meta_to_a_named_channel(tmp_path, bot_env, monkeypatch, capsys):
    """--as photo: mp3, картинка и <имя>-post.md — картинка с постом, затем аудио."""
    from narrator.cli import main

    channels_file(tmp_path, monkeypatch)
    folder = tmp_path / "tmp" / "mcp-patterns"
    folder.mkdir(parents=True)
    (folder / "mcp-patterns-audiobook.md").write_text("текст для озвучки, не пост")
    (folder / "mcp-patterns-image_prompt.md").write_text("Create a poster")
    send = ["bot", "send", str(folder), "--as", "photo"]

    assert main(send) == 1  # папке без meta.json канал называют
    assert "--channel" in capsys.readouterr().err
    assert main([*send, "--channel", "audio_article"]) == 4
    assert "нечего слать" in capsys.readouterr().err

    (folder / "Как устроены MCP-серверы (1.25x).mp3").write_bytes(b"ID3" + b"\0" * 64)
    (folder / "7a7593d2.png").write_bytes(b"png")
    (folder / "mcp-patterns-post.md").write_text(POST, encoding="utf-8")
    assert main([*send, "--channel", "audio_article"]) == 0
    assert bot_env.methods() == ["sendPhoto", "sendAudio"]
    _, photo, files = bot_env.calls[0]
    assert photo["chat_id"] == -1003755616451 and photo["message_thread_id"] == 14
    assert photo["caption"].startswith("<b>Аудиокнига: паттерны MCP</b>")
    assert '<a href="https://arxiv.org/abs/1">Статья</a>' in photo["caption"]
    assert files == ["photo"]
    _, audio, files = bot_env.calls[1]
    assert audio["title"] == "Аудиокнига: паттерны MCP" and "caption" not in audio
    assert audio["message_thread_id"] == 14 and files == ["audio"]
    assert sorted(p.name for p in folder.iterdir())[0] == "7a7593d2.png"  # файлы не тронуты

    assert main([*send, "--channel", "audio_article"]) == 0  # туда уже уходила
    assert "уже отправлена" in capsys.readouterr().out
    assert len(bot_env.calls) == 2
    assert main([*send, "--channel", "audio_article", "--force"]) == 0
    assert len(bot_env.calls) == 4


def test_cli_send_a_folder_goes_as_one_video_post_like_the_digest(
    tmp_path, bot_env, monkeypatch, capsys
):
    """По умолчанию папка уходит как дайджест новостей: видео, пост — подпись под ним."""
    import subprocess

    from PIL import Image

    from narrator.cli import main

    channels_file(tmp_path, monkeypatch)
    folder = tmp_path / "tmp" / "mcp-patterns"
    folder.mkdir(parents=True)
    silence = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono"]
    subprocess.run([*silence, "-t", "2", str(folder / "Как устроены (1.25x).mp3")], check=True)
    Image.new("RGB", (1025, 683), (40, 120, 200)).save(folder / "7a7593d2.png")
    (folder / "mcp-patterns-post.md").write_text(POST, encoding="utf-8")
    send = ["bot", "send", str(folder), "--channel", "audio_article"]

    assert main(send) == 0
    assert "видео: mcp-patterns.mp4" in capsys.readouterr().out
    assert bot_env.methods() == ["sendVideo"]  # один пост
    _, params, files = bot_env.calls[0]
    assert params["chat_id"] == -1003755616451 and params["message_thread_id"] == 14
    assert params["caption"].startswith("<b>Аудиокнига: паттерны MCP</b>")
    assert (params["width"], params["height"]) == (1024, 682) and params["duration"] == 2
    assert params["cover"] == "attach://cover" and files == ["cover", "thumbnail", "video"]
    assert (folder / "7a7593d2.png").exists()  # картинка на месте, кадры видео — рядом

    # второй раз картинкой считается та же, а не собранные кадры видео
    assert telegram.find_parcel(folder).image.name == "7a7593d2.png"
    assert main([*send, "--force"]) == 0
    assert bot_env.methods() == ["sendVideo", "sendVideo"]

    (folder / "7a7593d2.png").unlink()  # без картинки видео не собрать
    assert main([*send, "--force"]) == 0
    assert "видео не собрать" in capsys.readouterr().out
    assert bot_env.methods()[-1] == "sendAudio"


def test_folder_without_image_sends_audio_with_the_post_as_caption(tmp_path, monkeypatch):
    channels_file(tmp_path, monkeypatch)
    folder = tmp_path / "parcel"
    folder.mkdir()
    (folder / "talk.mp3").write_bytes(b"ID3")
    (folder / "post.md").write_text(POST, encoding="utf-8")
    transport = FakeTransport()
    state = State()
    done = telegram.send_folder(Bot("t", transport), state, folder, "voice_news", 465.5)
    assert [d.ok for d in done] == [True]
    assert transport.methods() == ["sendAudio"]
    params = transport.calls[0][1]
    assert params["caption"].startswith("<b>Аудиокнига") and params["duration"] == 465

    (folder / "post.md").write_text("я" * 1100, encoding="utf-8")
    with pytest.raises(telegram.TelegramError, match="1100 знаков"):
        telegram.send_folder(Bot("t", transport), state, folder, "voice_news")


def test_cli_send_to_a_named_channel(tmp_path, bot_env, monkeypatch, capsys):
    from narrator.cli import main

    lib = tmp_path / "library"
    channels_file(tmp_path, monkeypatch)
    article(lib)
    send = ["bot", "send", "2026-09-18/triple-debt", "--channel"]

    assert main([*send, "audio_article"]) == 0
    assert bot_env.sent_to("sendAudio") == [-1003755616451]
    assert main([*send, "audio_article"]) == 0  # туда уже уходила
    assert "уже отправлена" in capsys.readouterr().out
    assert main([*send, "voice_news"]) == 0  # а в другой канал — нет
    assert len(bot_env.sent_to("sendAudio")) == 2

    assert main([*send, "podcast"]) == 4
    assert "канала podcast нет" in capsys.readouterr().err
    assert len(bot_env.sent_to("sendAudio")) == 2


def test_cli_channels_lists_names_and_what_goes_where(tmp_path, monkeypatch, capsys):
    from narrator.cli import main

    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path / "library"))
    assert main(["bot", "channels"]) == 4
    assert "telegram_tems.md" in capsys.readouterr().err

    channels_file(tmp_path, monkeypatch)
    assert main(["bot", "channels"]) == 0
    out = capsys.readouterr().out
    assert "AUDIO_ARTICLE" in out and "-1003755616451_14" in out
    assert "HISTORY_CHAN" in out and "озвученные статьи" in out
    assert "VOICE_NEWS" in out and "дайджест новостей" in out

    channels_file(tmp_path, monkeypatch, "AUDIO_ARTICLE=-1003755616451_14\n")
    assert main(["bot", "channels"]) == 4  # обычные каналы не записаны
    assert "HISTORY_CHAN, VOICE_NEWS" in capsys.readouterr().err


def test_cli_subscribers_reports_unwritable_state(tmp_path, monkeypatch, capsys):
    from narrator.cli import main

    lib = tmp_path / "library"
    lib.write_text("библиотека — файл, состояние бота рядом не положить")
    monkeypatch.setenv("NARRATOR_LIBRARY", str(lib))
    monkeypatch.setenv(telegram.TOKEN_ENV, "111:AAAA")
    monkeypatch.setattr(telegram, "http_transport", lambda *a, **k: FakeTransport())
    assert main(["bot", "subscribers"]) == 4
    assert "telegram.json" in capsys.readouterr().err


def test_cli_send_without_token_reports_instead_of_crash(tmp_path, monkeypatch, capsys):
    from narrator.cli import main

    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path))
    monkeypatch.delenv(telegram.TOKEN_ENV, raising=False)
    monkeypatch.setenv(telegram.TOKEN_FILE_ENV, str(tmp_path / "нет.md"))
    article(tmp_path)
    assert main(["bot", "send", "2026-09-18/triple-debt"]) == 4
    assert "токен" in capsys.readouterr().err


def test_notify_after_synth_sends_to_subscribers(tmp_path, bot_env, capsys):
    from narrator.cli import notify_subscribers

    path, _ = article(tmp_path)
    state = State()
    state.upsert(42, "Аня")
    state.save(telegram.state_path(tmp_path))
    notify_subscribers(path, "silero")
    assert bot_env.sent_to("sendAudio") == [42]
    assert "отправлена 1 из 1" in capsys.readouterr().out


def test_notify_sends_to_channel_without_subscribers(tmp_path, bot_env, monkeypatch, capsys):
    from narrator.cli import notify_subscribers

    monkeypatch.setenv(telegram.CHANNEL_ENV, "-1002629071990")
    path, _ = article(tmp_path)
    notify_subscribers(path, "silero")
    assert bot_env.sent_to("sendAudio") == [-1002629071990]
    assert "отправлена 1 из 1" in capsys.readouterr().out


def test_notify_survives_telegram_failure(tmp_path, bot_env, capsys):
    from narrator.cli import notify_subscribers

    path, _ = article(tmp_path)
    state = State()
    state.upsert(42, "Аня")
    state.save(telegram.state_path(tmp_path))
    bot_env.replies["getUpdates"] = {"ok": False, "error_code": 401, "description": "Unauthorized"}
    notify_subscribers(path, "silero")
    assert "Unauthorized" in capsys.readouterr().err


# --- догнать неотправленное ----------------------------------------------


def second_article(tmp_path: Path) -> Path:
    path = tmp_path / "2026" / "09" / "19" / "second"
    path.mkdir(parents=True)
    Entry(
        title="Вторая",
        added="2026-09-19",
        engine="silero",
        versions={"silero": 60.0},
        license="own",
    ).save(path / "meta.json")
    (path / "2026-09-19-second_silero.mp3").write_bytes(b"\0" * 16)
    return path


def test_unsent_skips_sent_and_articles_without_audio(tmp_path):
    first, _ = article(tmp_path)
    second_article(tmp_path)
    silent = tmp_path / "2026" / "09" / "20" / "silent"
    silent.mkdir(parents=True)
    Entry(title="Без аудио", added="2026-09-20").save(silent / "meta.json")
    state = State()
    ids = [p.name for p, _, _ in telegram.unsent(state, tmp_path)]
    assert ids == ["triple-debt", "second"]
    telegram.mark_sent(state, first, "silero")
    assert [p.name for p, _, _ in telegram.unsent(state, tmp_path)] == ["second"]


def test_cli_unsent_sends_all_then_nothing(tmp_path, bot_env, monkeypatch, capsys):
    from narrator.cli import main

    monkeypatch.setenv(telegram.CHANNEL_ENV, "-100")
    article(tmp_path)
    second_article(tmp_path)
    assert main(["bot", "unsent"]) == 0
    assert bot_env.sent_to("sendAudio") == [-100, -100]
    assert main(["bot", "unsent"]) == 0
    assert "неотправленных озвучек нет" in capsys.readouterr().out
    assert bot_env.sent_to("sendAudio") == [-100, -100]


def test_cli_unsent_list_and_mark_send_nothing(tmp_path, bot_env, monkeypatch, capsys):
    from narrator.cli import main

    monkeypatch.setenv(telegram.CHANNEL_ENV, "-100")
    article(tmp_path)
    assert main(["bot", "unsent", "--list"]) == 0
    assert "triple-debt" in capsys.readouterr().out
    assert main(["bot", "unsent", "--mark"]) == 0
    assert main(["bot", "unsent"]) == 0
    assert "неотправленных озвучек нет" in capsys.readouterr().out
    assert bot_env.sent_to("sendAudio") == []


def test_cli_unsent_stops_on_failure_and_keeps_progress(tmp_path, bot_env, monkeypatch, capsys):
    from narrator.cli import main

    monkeypatch.setenv(telegram.CHANNEL_ENV, "-100")
    article(tmp_path)
    second_article(tmp_path)
    down = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
    bot_env.replies["sendAudio"] = [{"ok": True, "result": {}}, down]
    assert main(["bot", "unsent"]) == 4
    assert "Bad Gateway" in capsys.readouterr().err
    state = State.load(telegram.state_path(tmp_path))
    assert list(state.sent) == ["2026-09-18-triple-debt_silero"]
