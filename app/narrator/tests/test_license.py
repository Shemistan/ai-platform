"""Право на переработку статьи: без него в канал — только аннотация со ссылкой (CMP-005)."""

import json

import pytest

from narrator import library, telegram
from narrator.library import Entry


@pytest.fixture(autouse=True)
def subscribers_only(monkeypatch):
    monkeypatch.setenv(telegram.CHANNEL_ENV, "off")


def voiced(tmp_path, license="none", url="https://x.test/a"):
    path = tmp_path / "2026" / "09" / "18" / "foreign"
    path.mkdir(parents=True)
    entry = Entry(
        title="Чужая статья",
        added="2026-09-18",
        url=url,
        author="Автор",
        summary="Длинное описание статьи. " * 20,
        engine="silero",
        versions={"silero": 60.0},
        license=license,
    )
    entry.save(path / "meta.json")
    (path / "source.md").write_text("слово " * 200, encoding="utf-8")
    (path / library.audio_name(path, "silero")).write_bytes(b"ID3")
    return path, entry


def bot_with_subscriber():
    calls = []

    def transport(method, params, files=None):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 1}}

    state = telegram.State()
    state.upsert(42)
    return telegram.Bot("t", transport), state, calls


def test_default_license_is_none_and_validated(tmp_path):
    path, entry = voiced(tmp_path)
    assert Entry.load(path / "meta.json").license == "none"
    data = json.loads((path / "meta.json").read_text(encoding="utf-8")) | {"license": "free"}
    (path / "meta.json").write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="license"):
        Entry.load(path / "meta.json")


def test_full_retelling_without_license_is_refused(tmp_path):
    path, entry = voiced(tmp_path)
    bot, state, calls = bot_with_subscriber()
    with pytest.raises(telegram.TelegramError, match="права на переработку нет"):
        telegram.broadcast(bot, state, path, entry, "silero")
    assert calls == []
    assert telegram.is_fresh(state, path, "silero"), "не отмечено отправленным"


def test_annotation_is_short_and_links_original(tmp_path):
    path, entry = voiced(tmp_path)
    bot, state, calls = bot_with_subscriber()
    done = telegram.broadcast(bot, state, path, entry, "silero", annotation=True)
    assert [d.ok for d in done] == [True]
    ((method, params),) = calls
    assert method == "sendMessage"
    assert '<a href="https://x.test/a">Оригинал статьи</a>' in params["text"]
    summary = params["text"].split("\n\n")[1]
    assert len(summary) <= len("слово " * 200) * telegram.ANNOTATION_SHARE
    no_url, entry = voiced(tmp_path / "b", url=None)
    with pytest.raises(telegram.TelegramError, match="ссылка на оригинал"):
        telegram.broadcast(bot, state, no_url, entry, "silero", annotation=True)


def test_licensed_article_goes_in_full(tmp_path):
    path, entry = voiced(tmp_path, license="cc-by")
    bot, state, calls = bot_with_subscriber()
    telegram.broadcast(bot, state, path, entry, "silero")
    assert [c[0] for c in calls] == ["sendAudio"]


def test_bridge_saves_license(tmp_path, monkeypatch):
    from narrator import bridge

    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path))
    path, _ = voiced(tmp_path)
    article_id = library.article_id(path)
    assert bridge.article_save(article_id, {"license": "permission"})["license"] == "permission"
    with pytest.raises(bridge.BridgeError):
        bridge.article_save(article_id, {"license": "free"})
