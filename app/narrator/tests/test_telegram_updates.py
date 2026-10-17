"""`bot serve --updates`: нажатия кнопок и ответы боту уходят платформе строкой (PRD-001)."""

import json

import pytest

from narrator import telegram
from narrator.telegram import Bot, State

CLICK = {
    "update_id": 7,
    "callback_query": {"id": "q1", "from": {"id": 42}, "data": "rv:pub:abc:123"},
}


@pytest.fixture(autouse=True)
def no_channel(monkeypatch):
    monkeypatch.setenv(telegram.CHANNEL_ENV, "off")


class FakeTransport:
    """Вместо сети: копит вызовы, getUpdates отдаёт по очереди из replies."""

    def __init__(self, replies: dict | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.replies = replies or {}

    def __call__(self, method: str, params: dict, files: dict | None = None) -> dict:
        self.calls.append((method, params))
        reply = self.replies.get(
            method, {"ok": True, "result": [] if method == "getUpdates" else {}}
        )
        if isinstance(reply, list):
            reply = reply.pop(0) if reply else {"ok": True, "result": []}
        return reply

    def sent_to(self, method: str) -> list[int]:
        return [p["chat_id"] for m, p in self.calls if m == method]


def message(update_id: int, chat_id: int, text: str) -> dict:
    return {
        "update_id": update_id,
        "message": {"chat": {"id": chat_id, "first_name": "Аня"}, "text": text},
    }


def reply(update_id: int, chat_id: int, text: str, to: int) -> dict:
    update = message(update_id, chat_id, text)
    update["message"]["reply_to_message"] = {"message_id": to}
    return update


@pytest.fixture
def bot_env(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path))
    monkeypatch.setenv(telegram.TOKEN_ENV, "111:AAAA")
    transport = FakeTransport()
    monkeypatch.setattr(telegram, "http_transport", lambda *a, **k: transport)
    return transport


def test_pull_hands_clicks_and_replies_to_on_update_without_hint():
    updates = [CLICK, reply(8, 42, "Новая подпись", 1001), message(9, 43, "/start")]
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": updates}]})
    got: list[dict] = []
    state = State()
    fresh = telegram.pull(Bot("t", transport), state, on_update=got.append)
    assert [u["update_id"] for u in got] == [7, 8]
    assert [s.chat_id for s in fresh] == [43]
    assert transport.sent_to("sendMessage") == [43], "на нажатие и ответ подсказку не шлём"
    assert state.offset == 10
    assert transport.calls[0][1]["allowed_updates"] == ["message", "callback_query"]


def test_pull_without_on_update_keeps_old_behaviour():
    transport = FakeTransport({"getUpdates": [{"ok": True, "result": [reply(3, 42, "текст", 5)]}]})
    telegram.pull(Bot("t", transport), State())
    assert transport.calls[0][1]["allowed_updates"] == ["message"]
    assert transport.sent_to("sendMessage") == [42]  # обычная подсказка


def test_cli_serve_updates_prints_json_lines(tmp_path, bot_env, capsys):
    from narrator.cli import main

    bot_env.replies["getMe"] = {"ok": True, "result": {"username": "demo_bot"}}
    bot_env.replies["getUpdates"] = [{"ok": True, "result": [CLICK]}]
    assert main(["bot", "serve", "--once", "--updates"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("update ")]
    assert len(lines) == 1
    assert json.loads(lines[0][len("update ") :]) == CLICK
