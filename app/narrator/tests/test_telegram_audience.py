"""Подписчики бота: источник подписки, дата отписки и отчёт без ПДн (AN-011)."""

import json

import pytest

from narrator import cli, library, telegram
from narrator.telegram import Bot, State


class FakeTransport:
    def __init__(self, updates: list[dict]):
        self.updates = updates
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, params: dict, files: dict | None = None) -> dict:
        self.calls.append((method, params))
        if method == "getUpdates":
            return {"ok": True, "result": self.updates}
        return {"ok": True, "result": {}}


def message(update_id: int, chat_id: int, text: str) -> dict:
    chat = {"id": chat_id, "type": "private", "first_name": "Аня"}
    return {"update_id": update_id, "message": {"chat": chat, "text": text}}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.setattr(telegram, "_LIMITER", telegram.ReplyLimiter(interval=60))
    monkeypatch.setattr(telegram, "serve_running", lambda root=None: False)
    for name in (telegram.INVITE_ENV, telegram.MAX_SUBSCRIBERS_ENV, telegram.BOT_USERNAME_ENV):
        monkeypatch.delenv(name, raising=False)


def pull(state: State, *texts: str, chat_id: int = 42, invite: str | None = None) -> None:
    updates = [message(state.offset + i + 1, chat_id, t) for i, t in enumerate(texts)]
    telegram.pull(Bot("t", FakeTransport(updates)), state, invite=invite)


def test_start_payload_saved_as_start_param():
    state = State()
    pull(state, "/start abc123")
    assert state.find(42).start_param == "abc123"
    # Не по правилам Telegram (кириллица, пробелы в одном слове нет, длина) — метки нет.
    pull(state, "/start проект", chat_id=43)
    assert state.find(43).start_param is None


def test_start_payload_with_invite():
    state = State()
    pull(state, "/start Код-1__a1b2c3", chat_id=1, invite="Код-1")
    pull(state, "/start Код-1", chat_id=2, invite="Код-1")
    pull(state, "/start чужой__a1b2c3", chat_id=3, invite="Код-1")
    assert state.find(1).start_param == "a1b2c3"
    assert state.find(2).start_param is None
    assert state.find(3) is None, "неверный код — не подписан"


def test_stop_writes_deactivated_at_and_restart_clears(monkeypatch):
    days = iter(["2026-10-01", "2026-10-05", "2026-10-09"])
    monkeypatch.setattr(library, "today", lambda: next(days))
    state = State()
    pull(state, "/start post-1")
    pull(state, "/stop")
    sub = state.find(42)
    assert (sub.active, sub.deactivated_at) == (False, "2026-10-05")
    # Строка для retention — без chat_id и имени.
    assert state.departed == [{"added": "2026-10-01", "source": "post-1", "left": "2026-10-05"}]
    pull(state, "/start post-2")
    sub = state.find(42)
    assert (sub.active, sub.deactivated_at, sub.added, sub.start_param) == (
        True,
        None,
        "2026-10-09",
        "post-2",
    )


def test_old_telegram_json_reads(tmp_path):
    path = tmp_path / "telegram.json"
    old = {
        "offset": 5,
        "subscribers": [
            {"chat_id": 42, "name": "Аня", "added": "2026-09-18", "active": True, "note": ""},
            {
                "chat_id": 43,
                "name": "Боря",
                "added": "2026-09-19",
                "active": False,
                "note": "отписался",
            },
        ],
        "sent": {"k": "2026-09-18T10:00:00"},
    }
    path.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    state = State.load(path)
    assert (state.find(42).start_param, state.find(42).deactivated_at, state.departed) == (
        None,
        None,
        [],
    )
    state.save(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["subscribers"][1]["name"] == "Боря" and saved["departed"] == []


def test_departed_merges_between_processes(tmp_path):
    path = tmp_path / "telegram.json"
    base = State()
    base.upsert(1, "")
    base.upsert(2, "")
    base.save(path)
    a, b = State.load(path), State.load(path)
    a.deactivate(1, "/stop")
    b.deactivate(2, "blocked")
    a.save(path)
    b.save(path)
    assert len(State.load(path).departed) == 2


def test_audience_aggregates_without_pii():
    state = State()
    for chat_id, added, source in (
        (1, "2026-09-01", "p1"),
        (2, "2026-09-02", "p1"),
        (3, "2026-09-03", None),
    ):
        state.subscribers.append(
            telegram.Subscriber(chat_id=chat_id, name="x", added=added, start_param=source)
        )
    state.departed = [
        {"added": "2026-09-01", "source": "p1", "left": "2026-09-04"},  # ушёл до D7
        {"added": "2026-09-02", "source": "p2", "left": "2026-09-20"},  # дожил до D7, не до D30
        {"added": "2026-10-10", "source": "p2", "left": "2026-10-11"},  # D7 ещё не наступил
    ]
    report = telegram.audience(state, today="2026-10-12")
    assert report["active"] == 3
    assert report["by_source"]["p1"] == {"subscribed": 3, "active": 2}
    assert report["by_source"]["p2"] == {"subscribed": 2, "active": 0}
    assert report["by_source"]["—"] == {"subscribed": 1, "active": 1}
    first, last = report["cohorts"][0], report["cohorts"][-1]
    assert (first["cohort"], first["n"], first["d7"], first["d30"]) == ("2026-08-31", 5, 0.8, 0.6)
    assert (last["cohort"], last["n"], last["d7"], last["d7_n"]) == ("2026-10-05", 1, None, 0)
    assert "chat_id" not in json.dumps(report) and "name" not in json.dumps(report)


def test_cli_audience(tmp_path, monkeypatch, capsys):
    state = State()
    state.subscribers.append(
        telegram.Subscriber(chat_id=777001, name="Аня", added="2026-09-01", start_param="p1")
    )
    path = tmp_path / "telegram.json"
    state.save(path)
    monkeypatch.setattr(telegram, "state_path", lambda root=None: path)
    assert cli.main(["bot", "audience"]) == 0
    out = capsys.readouterr().out
    assert "p1" in out and "777001" not in out and "Аня" not in out


def test_start_link_and_post(monkeypatch):
    assert telegram.start_link("abc", username="") is None
    assert (
        telegram.start_link("abc", username="@my_bot", invite="") == "https://t.me/my_bot?start=abc"
    )
    assert (
        telegram.start_link("abc", username="my_bot", invite="K1")
        == "https://t.me/my_bot?start=K1__abc"
    )
    assert telegram.start_tag("2026-10-01 go-gc!") == "2026-10-01-go-gc"
    assert len(telegram.start_tag("сборщик мусора")) == 12
    _, source = telegram.start_source(telegram.start_tag("2026-10-01 go-gc!"))
    assert source == "2026-10-01-go-gc"
