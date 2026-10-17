"""Повторы запросов к Bot API и темп рассылки (BE-012) — на фейковом транспорте и часах."""

from pathlib import Path

import pytest

from narrator import telegram
from narrator.library import Entry
from narrator.telegram import Bot, State


@pytest.fixture(autouse=True)
def no_channel(monkeypatch):
    """Рассылка — подписчикам в личку, как в test_telegram."""
    monkeypatch.setenv(telegram.CHANNEL_ENV, "off")


class FakeClock:
    """Часы, которые двигает только sleep: паузы в тесте мгновенные и видны в sleeps."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class Replies:
    """Транспорт с ответами по очереди; исключение в очереди — бросается."""

    def __init__(self, *queue):
        self.queue = list(queue)
        self.calls: list[str] = []

    def __call__(self, method: str, params: dict, files: dict | None = None) -> dict:
        self.calls.append(method)
        reply = self.queue.pop(0) if self.queue else {"ok": True, "result": {"message_id": 1}}
        if isinstance(reply, Exception):
            raise reply
        return reply


def bot(transport, clock: FakeClock | None = None) -> Bot:
    clock = clock or FakeClock()
    return Bot("t", transport, sleep=clock.sleep, clock=clock)


def article(tmp_path: Path) -> tuple[Path, Entry]:
    path = tmp_path / "2026" / "09" / "18" / "triple-debt"
    path.mkdir(parents=True)
    entry = Entry(
        title="Тройной долг",
        added="2026-09-18",
        url="https://x.test/a",
        summary="Технический долг.",
        author="Аноним",
        voice="eugene",
        engine="silero",
        duration_sec=125.0,
        versions={"silero": 125.0},
        license="permission",  # право на переработку есть — полный пересказ уходит
    )
    entry.save(path / "meta.json")
    (path / "2026-09-18-triple-debt_silero.mp3").write_bytes(b"\0" * 2048)
    return path, entry


TOO_MANY = {
    "ok": False,
    "error_code": 429,
    "description": "Too Many Requests: retry after 3",
    "parameters": {"retry_after": 3},
}


def test_429_on_send_message_waits_retry_after_and_sends():
    transport = Replies(TOO_MANY, {"ok": True, "result": {"message_id": 7}})
    clock = FakeClock()
    reply = bot(transport, clock).request("sendMessage", {"chat_id": 42, "text": "привет"})
    assert reply.ok and reply.result == {"message_id": 7}
    assert transport.calls == ["sendMessage", "sendMessage"]
    assert 3 in clock.sleeps


def test_429_with_huge_retry_after_gives_up():
    huge = TOO_MANY | {"parameters": {"retry_after": telegram.MAX_RETRY_AFTER_S + 1}}
    transport = Replies(huge)
    reply = bot(transport).request("sendMessage", {"chat_id": 42})
    assert not reply.ok and reply.code == 429
    assert transport.calls == ["sendMessage"]


def test_send_video_timeout_is_not_retried():
    transport = Replies(telegram.TelegramError("sendVideo: timed out"))
    with pytest.raises(telegram.TelegramError, match="timed out"):
        bot(transport).request("sendVideo", {"chat_id": 42}, {})
    assert transport.calls == ["sendVideo"], "после таймаута видео могло уйти — без повтора"


def test_send_retried_when_request_did_not_leave():
    transport = Replies(telegram.NotSent("sendAudio: connection refused"))
    assert bot(transport).request("sendAudio", {"chat_id": 42}).ok
    assert transport.calls == ["sendAudio", "sendAudio"]


def test_send_5xx_is_not_retried():
    transport = Replies({"ok": False, "error_code": 500, "description": "Internal"})
    reply = bot(transport).request("sendPhoto", {"chat_id": 1})
    assert not reply.ok and transport.calls == ["sendPhoto"]


def test_safe_methods_retried_on_5xx_and_timeout():
    transport = Replies(
        {"ok": False, "error_code": 502, "description": "Bad Gateway"}, {"ok": True, "result": 5}
    )
    assert bot(transport).call("getChatMemberCount", {"chat_id": 1}) == 5
    timeout = telegram.TelegramError("timed out")
    transport = Replies(timeout, timeout, {"ok": True, "result": {"id": 1}})
    assert bot(transport).call("getChat", {"chat_id": 1}) == {"id": 1}
    assert len(transport.calls) == telegram.ATTEMPTS


def test_retries_end_after_attempts():
    down = {"ok": False, "error_code": 502, "description": "Bad Gateway"}
    transport = Replies(*[down] * 5)
    with pytest.raises(telegram.TelegramError, match="Bad Gateway"):
        bot(transport).call("getMe")
    assert len(transport.calls) == telegram.ATTEMPTS


def test_broadcast_keeps_send_rate(tmp_path):
    path, entry = article(tmp_path)
    clock = FakeClock()
    stamps = []

    def transport(method, params, files=None):
        stamps.append(clock.now)
        return {"ok": True, "result": {"message_id": 1}}

    state = State()
    for n in range(100):
        state.upsert(1000 + n, f"подписчик {n}")
    done = telegram.broadcast(bot(transport, clock), state, path, entry, "silero")
    assert len(done) == 100 and all(d.ok for d in done)
    rate = telegram.SEND_RATE
    windows = [stamps[i + rate] - stamps[i] for i in range(len(stamps) - rate)]
    assert min(windows) >= 1.0 - 1e-9, "в любой секунде не больше SEND_RATE отправок"
