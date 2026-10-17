"""Состояние бота без гонок (BE-002): атомарный save, слияние правок процессов, один getUpdates."""

import fcntl
import json
import os
import stat

import pytest

from narrator import telegram
from narrator.telegram import Bot, State


def test_save_is_atomic_and_private(tmp_path, monkeypatch):
    path = tmp_path / "telegram.json"
    state = State()
    state.upsert(42)
    state.save(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_text(encoding="utf-8")

    def boom(*args, **kwargs):
        raise OSError("диск полон")

    state.upsert(43)
    monkeypatch.setattr(telegram.os, "replace", boom)
    with pytest.raises(OSError):
        state.save(path)
    assert path.read_text(encoding="utf-8") == before, "прежний файл цел"
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")], "tmp убран"


def test_serve_does_not_lose_sent_from_other_process(tmp_path):
    path = tmp_path / "telegram.json"
    State().save(path)
    serve = State.load(path)  # `bot serve` держит состояние в памяти
    send = State.load(path)  # `bot send` в другом процессе
    send.sent["2026-09-18-x_silero"] = "2026-09-18T10:00:00"
    send.remove(1)  # подписчика нет — ничего не меняет
    send.save(path)

    serve.offset = 77
    serve.upsert(42)
    serve.save(path)

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["sent"] == {"2026-09-18-x_silero": "2026-09-18T10:00:00"}
    assert data["offset"] == 77
    assert [s["chat_id"] for s in data["subscribers"]] == [42]
    assert serve.sent, "после save в памяти serve — слитое состояние"


def test_merge_keeps_unsubscribe_from_other_process(tmp_path):
    path = tmp_path / "telegram.json"
    first = State()
    first.upsert(42)
    first.save(path)
    serve = State.load(path)
    other = State.load(path)
    other.remove(42)  # бот заблокирован — запись удалена (CMP-006)
    other.save(path)
    serve.offset = 5  # подписчика 42 serve не трогал
    serve.save(path)
    assert State.load(path).subscribers == [], "удаление в другом процессе не воскресает"


def test_pull_skips_get_updates_while_serve_runs(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram.library, "library_root", lambda: tmp_path)
    calls = []

    def transport(method, params, files=None):
        calls.append(method)
        return {"ok": True, "result": []}

    lock = telegram.serve_lock_path()
    lock.write_text("123\n")
    with open(lock) as other:  # другой процесс держит serve
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert telegram.serve_running()
        assert telegram.pull(Bot("t", transport), State()) == []
        assert calls == []
        with pytest.raises(telegram.TelegramError, match="уже запущен"):
            with telegram.serving():
                pass
        fcntl.flock(other, fcntl.LOCK_UN)

    assert not telegram.serve_running()
    with telegram.serving():
        assert not telegram.serve_running(), "сам serve свой замок не считает чужим"
        telegram.pull(Bot("t", transport), State())
        assert lock.read_text().strip() == str(os.getpid())
    assert calls == ["getUpdates"]
