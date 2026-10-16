"""`narrate bridge …`: JSON для страниц платформы."""

import io
import json

import pytest

from narrator import cli, library, telegram
from narrator.library import Entry, create_entry


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "library"
    root.mkdir()
    monkeypatch.setenv("NARRATOR_LIBRARY", str(root))
    monkeypatch.setenv("NARRATOR_MODELS_DIR", str(tmp_path / "models"))
    monkeypatch.delenv("NARRATOR_TELEGRAM_TOKEN", raising=False)
    monkeypatch.setenv("NARRATOR_TELEGRAM_TOKEN_FILE", str(tmp_path / "нет-токена.md"))
    return root


def bridge(capsys, monkeypatch, *args, stdin=""):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(["bridge", *args])
    out = json.loads(capsys.readouterr().out)
    return code, out


def test_preview_normalizes_and_counts(capsys, monkeypatch, home):
    code, out = bridge(capsys, monkeypatch, "preview", stdin="Go 1.26 вышел 8 октября.\n")
    assert code == 0
    assert "Го" in out["script"] and "октября" in out["script"]
    assert out["changed"] == 1
    assert all(i["level"] == "warning" for i in out["issues"])


def test_preview_empty(capsys, monkeypatch, home):
    _, out = bridge(capsys, monkeypatch, "preview", stdin="  \n")
    assert out == {"script": "", "changed": 0, "issues": []}


def test_split_by_headings_and_rules(capsys, monkeypatch, home):
    text = "# Первая\n\nтекст один\n\n# Вторая\n\nтекст два\n"
    _, out = bridge(capsys, monkeypatch, "split", stdin=text)
    assert [a["title"] for a in out["articles"]] == ["Первая", "Вторая"]
    assert out["articles"][1]["text"].startswith("# Вторая")
    _, out = bridge(capsys, monkeypatch, "split", stdin="раз\n\n---\n\nдва\n")
    assert [a["title"] for a in out["articles"]] == ["раз", "два"]


def test_split_limit(capsys, monkeypatch, home):
    text = "".join(f"# Статья {n}\n\nтекст\n\n" for n in range(101))
    code, out = bridge(capsys, monkeypatch, "split", stdin=text)
    assert code == 1 and "не больше 100" in out["error"]


def test_article_new_save_and_library(capsys, monkeypatch, home):
    payload = json.dumps(
        {"title": "Очереди", "text": "Брокер — не база.", "url": "https://x.dev/q"}
    )
    code, out = bridge(capsys, monkeypatch, "article-new", stdin=payload)
    assert code == 0
    assert out["title"] == "Очереди" and out["script"].strip() == "Брокер — не база."
    assert out["has_script"] and out["stale"] and not out["has_audio"]
    article_id = out["id"]

    save = json.dumps({"summary": "О брокерах.", "script": "Новый текст."})
    _, out = bridge(capsys, monkeypatch, "article-save", article_id, stdin=save)
    assert out["summary"] == "О брокерах." and out["script"] == "Новый текст.\n"

    _, out = bridge(capsys, monkeypatch, "library")
    assert [a["id"] for a in out["articles"]] == [article_id]
    assert out["unsent"] == 0
    assert (home / "index.md").is_file()


def test_article_new_needs_title_and_text(capsys, monkeypatch, home):
    code, out = bridge(capsys, monkeypatch, "article-new", stdin=json.dumps({"title": "x"}))
    assert code == 1 and "название и текст" in out["error"]


def test_article_unknown(capsys, monkeypatch, home):
    code, out = bridge(capsys, monkeypatch, "article", "2026-01-01-nope")
    assert code == 1 and "нет в библиотеке" in out["error"]


def test_library_marks_voiced_unsent_and_check(capsys, monkeypatch, home):
    path = create_entry(home, Entry(title="Статья", added="2026-09-18", summary="о чём"))
    (path / "script.txt").write_text("текст\n", encoding="utf-8")
    audio = path / library.audio_name(path, "silero")
    audio.write_bytes(b"mp3")
    entry = Entry.load(path / "meta.json")
    entry.engine, entry.versions, entry.audio = "silero", {"silero": 12.0}, audio.name
    entry.save(path / "meta.json")
    (path / "check.md").write_text("# Проверка озвучки\n\nСовпадение по словам: 93%, блоков…\n")

    _, out = bridge(capsys, monkeypatch, "library")
    item = out["articles"][0]
    assert item["has_audio"] and not item["stale"] and item["check"] == 93
    assert not item["sent"] and out["unsent"] == 1
    _, out = bridge(capsys, monkeypatch, "unsent")
    assert out["unsent"][0]["engine"] == "silero"

    state = telegram.State()
    telegram.mark_sent(state, path, "silero")
    state.save(telegram.state_path())
    _, out = bridge(capsys, monkeypatch, "library")
    assert out["articles"][0]["sent"] and out["unsent"] == 0

    _, out = bridge(capsys, monkeypatch, "article", item["id"])
    assert out["audio"] == {"silero": str(audio)}
    assert "93%" in out["check_report"]


def test_issue_with_verdict(capsys, monkeypatch, home):
    folder = library.day_dir(home, "2026-09-24") / "digest"
    folder.mkdir(parents=True)
    (folder / "narration.md").write_text("пересказ\n", encoding="utf-8")
    (folder / "review.md").write_text("# Судья\n\nВердикт: не готово\n", encoding="utf-8")
    _, out = bridge(capsys, monkeypatch, "issue", "--date", "2026-09-24")
    assert out["exists"] and out["reviewed"] and out["verdict"] == "не готово"
    _, out = bridge(capsys, monkeypatch, "issue", "--date", "2026-09-25")
    assert not out["exists"] and out["verdict"] is None


def test_reviews_and_review(capsys, monkeypatch, home):
    from datetime import date

    path = library.youtube_dir(home, date(2026, 9, 25)) / "week_2026-09-19_2026-09-25"
    path.mkdir(parents=True)
    Entry(title="Неделя", added="2026-09-25", kind="weekly").save(path / "meta.json")
    (path / "teleprompter.md").write_text("суфлёр\n", encoding="utf-8")
    _, out = bridge(capsys, monkeypatch, "reviews")
    assert out["reviews"][0]["files"] == ["teleprompter.md"]
    _, out = bridge(capsys, monkeypatch, "review", out["reviews"][0]["id"])
    assert out["files"] == {"teleprompter.md": "суфлёр\n"}


def test_bot_without_token(capsys, monkeypatch, home):
    _, out = bridge(capsys, monkeypatch, "bot")
    assert out["token"] is False and "токен" in out["token_error"]


def test_subscribers_from_state(capsys, monkeypatch, home):
    state = telegram.State()
    state.upsert(42, "Маша")
    state.save(telegram.state_path())
    _, out = bridge(capsys, monkeypatch, "subscribers")
    assert out["active"] == 1 and out["subscribers"][0]["name"] == "Маша"


def test_voices_from_package(capsys, monkeypatch, home):
    _, out = bridge(capsys, monkeypatch, "voices")
    by_voice = {v["voice"]: v for v in out["voices"]}
    assert by_voice["M1"]["record"] is None
    assert by_voice["Charon"]["record"] == "gemini" and by_voice["Shem"]["record"] == "file"
    assert by_voice["Charon"]["sample"] and by_voice["Charon"]["seconds"] > 0
    assert not by_voice["Charon"]["custom"]


def test_engines_and_models(capsys, monkeypatch, home):
    _, out = bridge(capsys, monkeypatch, "engines")
    ids = [e["id"] for e in out["engines"]]
    assert ids[:3] == ["espeech", "silero", "supertonic"]
    silero = out["engines"][1]
    assert silero["problem"] == "нет модели Silero" and "eugene" in silero["voices"]
    assert out["engines"][0]["tunable"] and out["engines"][0]["default_steps"] == 32
    _, out = bridge(capsys, monkeypatch, "models")
    assert not out["root_exists"]
    assert {m["id"] for m in out["main"]} >= {"espeech", "silero", "whisper"}
    assert all(not m["exists"] for m in out["main"] if m["id"] != "vocos")


def test_sources_view(capsys, monkeypatch, home):
    _, out = bridge(capsys, monkeypatch, "sources")
    assert out["sources"] and all("authority" in s for s in out["sources"])
    _, out = bridge(capsys, monkeypatch, "failed")
    assert out == {"failed": []}
    _, out = bridge(capsys, monkeypatch, "top", "--date", "2026-09-24")
    assert out == {"day": "2026-09-24", "events": []}
