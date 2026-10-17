import json

import pytest

from narrator.library import (
    Entry,
    article_id,
    audio_name,
    create_entry,
    entries,
    format_duration,
    library_root,
    pending,
    render_index,
    slugify,
    write_index,
)


def test_slugify():
    assert slugify("Синтез речи: история и 2026 год") == "sintez-rechi-istoriya-i-2026-god"
    assert slugify("Speech Synthesis — Wikipedia") == "speech-synthesis-wikipedia"
    assert slugify("!!!") == "article"


def test_create_entry_dedupes_and_roundtrips(tmp_path):
    e = Entry(title="Статья", added="2026-09-18", url="https://x.test/a", summary="Про всё.")
    p1 = create_entry(tmp_path, e)
    p2 = create_entry(tmp_path, e)
    assert p1 == tmp_path / "2026" / "09" / "18" / "statya"
    assert p2 == tmp_path / "2026" / "09" / "18" / "statya-2"
    assert Entry.load(p1 / "meta.json") == e


def test_load_rejects_unknown_fields_and_quality(tmp_path):
    (tmp_path / "meta.json").write_text(json.dumps({"title": "t", "added": "d", "x": 1}))
    with pytest.raises(ValueError, match="неизвестные поля"):
        Entry.load(tmp_path / "meta.json")
    (tmp_path / "meta.json").write_text(
        json.dumps({"title": "t", "added": "d", "source_quality": "guess"})
    )
    with pytest.raises(ValueError, match="source_quality"):
        Entry.load(tmp_path / "meta.json")


def test_load_drops_non_http_url(tmp_path):
    # meta.json пишет агент по чужой статье: javascript: не должен стать ссылкой (SEC-003).
    (tmp_path / "meta.json").write_text(
        json.dumps({"title": "t", "added": "d", "url": "javascript:alert(1)"})
    )
    assert Entry.load(tmp_path / "meta.json").url is None
    assert Entry(title="t", added="d", url=" data:text/html,x").url is None
    assert Entry(title="t", added="d", url="http://x.test/a").url == "http://x.test/a"


def test_format_duration():
    assert format_duration(None) == "—"
    assert format_duration(65.4) == "1:05"
    assert format_duration(3725) == "1:02:05"


def test_index_newest_first_with_links(tmp_path):
    old = Entry(title="Старая | с чертой", added="2026-09-01", summary="Коротко.")
    new = Entry(
        title="Новая",
        added="2026-09-18",
        url="https://x.test/n",
        summary="О новом.",
        audio="2026-09-18-novaya_silero.mp3",
        versions={"espeech": 125, "silero": 100, "supertonic": 90},
        source_quality="webfetch",
    )
    create_entry(tmp_path, old)
    d = create_entry(tmp_path, new)
    (d / "2026-09-18-novaya_espeech.mp3").write_bytes(b"mp3")
    (d / "2026-09-18-novaya_silero.mp3").write_bytes(b"mp3")  # supertonic файла нет
    text = render_index(tmp_path, entries(tmp_path))
    rows = [line for line in text.splitlines() if line.startswith("| 2026")]
    assert rows[0].startswith("| 2026-09-18 | [Новая](https://x.test/n) | О новом. ⚠")
    assert (
        "[espeech](2026/09/18/novaya/2026-09-18-novaya_espeech.mp3) 2:05, "
        "[silero](2026/09/18/novaya/2026-09-18-novaya_silero.mp3) 1:40 | `2026/09/18/novaya/` |"
    ) in rows[0]
    assert "supertonic" not in rows[0]
    assert "Старая \\| с чертой" in rows[1]
    assert "не озвучена" in rows[1]
    assert write_index(tmp_path).read_text(encoding="utf-8") == text


def test_audio_name(tmp_path):
    assert audio_name(tmp_path / "2026" / "09" / "18" / "x", "silero") == "2026-09-18-x_silero.mp3"


def test_flat_dirs_move_into_day_folders(tmp_path):
    old = tmp_path / "2026-09-18-triple-debt"
    old.mkdir()
    Entry(title="Долг", added="2026-09-18", versions={"silero": 60.0}).save(old / "meta.json")
    (old / "2026-09-18-triple-debt_silero.mp3").write_bytes(b"mp3")
    (tmp_path / "_stress-test").mkdir()  # не статья — не трогаем
    [(path, e)] = entries(tmp_path)
    assert path == tmp_path / "2026" / "09" / "18" / "triple-debt"
    assert not old.exists() and (tmp_path / "_stress-test").exists()
    assert (path / audio_name(path, "silero")).exists()
    assert pending(tmp_path, "silero") == []


def test_legacy_audio_is_renamed(tmp_path):
    d = create_entry(
        tmp_path,
        Entry(title="Старая", added="2026-09-18", audio="audio.mp3", engine="espeech",
              duration_sec=60.0),
    )  # fmt: skip
    (d / "audio.mp3").write_bytes(b"mp3")
    [(_, e)] = entries(tmp_path)
    assert not (d / "audio.mp3").exists()
    assert (d / "2026-09-18-staraya_espeech.mp3").exists()
    assert e.audio == "2026-09-18-staraya_espeech.mp3" and e.versions == {"espeech": 60.0}
    assert Entry.load(d / "meta.json").versions == {"espeech": 60.0}


def test_pending(tmp_path):
    import os

    no_script = create_entry(tmp_path, Entry(title="Без текста", added="2026-09-01"))
    no_audio = create_entry(tmp_path, Entry(title="Без аудио", added="2026-09-02"))
    (no_audio / "script.txt").write_text("Текст.", encoding="utf-8")
    done = create_entry(tmp_path, Entry(title="Готова", added="2026-09-03"))
    (done / "script.txt").write_text("Текст.", encoding="utf-8")
    mp3 = done / audio_name(done, "espeech")
    mp3.write_bytes(b"mp3")
    os.utime(done / "script.txt", (1, 1))  # аудио новее текста

    assert pending(tmp_path, "espeech") == [no_audio]
    assert pending(tmp_path, "silero") == [no_audio, done]  # у silero файла нет
    assert pending(tmp_path, "espeech", force=True) == [no_audio, done]
    assert no_script not in pending(tmp_path, "espeech", force=True)

    os.utime(done / "script.txt", None)  # текст поправили после озвучки
    os.utime(mp3, (1, 1))
    assert pending(tmp_path, "espeech") == [no_audio, done]


def test_library_root_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path))
    assert library_root() == tmp_path.resolve()


def test_library_root_points_to_main_checkout(monkeypatch):
    monkeypatch.delenv("NARRATOR_LIBRARY", raising=False)
    root = library_root()
    assert root.name == "library"
    assert root.parent.name == "narrator"  # библиотека — у движка, app/narrator/library
    # Из worktree — всё равно основной checkout, не .claude/worktrees/…
    assert ".claude" not in root.parts


def test_pending_separates_articles_and_digests(tmp_path):
    article = create_entry(tmp_path, Entry(title="Статья", added="2026-09-22"))
    daily = create_entry(tmp_path, Entry(title="Дайджест", added="2026-09-23", kind="digest"))
    weekly = create_entry(tmp_path, Entry(title="Неделя", added="2026-09-23", kind="weekly"))
    for d in (article, daily, weekly):
        (d / "script.txt").write_text("Текст.", encoding="utf-8")
    assert pending(tmp_path, "espeech") == [article]
    assert pending(tmp_path, "espeech", kinds=("digest",)) == [daily]


def test_day_folders_move_into_year_month_day(tmp_path):
    old = tmp_path / "2026-09-18"
    for slug in ("triple-debt", "digest"):
        (old / slug).mkdir(parents=True)
        Entry(title=slug, added="2026-09-18").save(old / slug / "meta.json")
    (old / "triple-debt" / "2026-09-18-triple-debt_silero.mp3").write_bytes(b"mp3")
    week = tmp_path / "youtube_news" / "week_2026-09-26_2026-10-02"
    month = tmp_path / "youtube_news" / "month_2026-09"
    for d, kind in ((week, "weekly"), (month, "monthly")):
        d.mkdir(parents=True)
        Entry(title=d.name, added="2026-10-02", kind=kind).save(d / "meta.json")

    found = {article_id(p): p for p, _ in entries(tmp_path)}

    assert not old.exists()
    new = tmp_path / "2026" / "09" / "18" / "triple-debt"
    assert found["2026-09-18-triple-debt"] == new  # имя статьи то же — mp3 и отметки на месте
    assert (new / audio_name(new, "silero")).exists()
    assert found["2026-09-18-digest"] == tmp_path / "2026" / "09" / "18" / "digest"
    youtube = tmp_path / "youtube_news"
    assert found["youtube_news-week_2026-09-26_2026-10-02"] == youtube / "2026" / "10" / week.name
    assert found["youtube_news-month_2026-09"] == youtube / "2026" / "09" / month.name
