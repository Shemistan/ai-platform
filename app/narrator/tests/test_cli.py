import json
from pathlib import Path

import pytest

from narrator.cli import main
from narrator.library import article_id


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path))
    return tmp_path


def test_new_lint_and_index(lib, capsys):
    assert main(["new", "--title", "Проба статьи", "--url", "https://x.test/a"]) == 0
    path = Path(capsys.readouterr().out.strip())
    meta = json.loads((path / "meta.json").read_text(encoding="utf-8"))
    assert meta["source_quality"] == "pasted"
    assert "Проба статьи" in (lib / "index.md").read_text(encoding="utf-8")

    (path / "script.txt").write_text("В 2026 году всё хорошо.\n", encoding="utf-8")
    assert path.parents[3] == lib.resolve()  # library/<год>/<месяц>/<день>/<статья>
    assert main(["lint", "/".join(path.parts[-4:])]) == 1
    (path / "script.txt").write_text("В этом году всё хорошо.\n", encoding="utf-8")
    assert main(["lint", str(path)]) == 0


def test_synth_requires_summary(lib, capsys):
    main(["new", "--title", "Без описания"])
    path = Path(capsys.readouterr().out.strip())
    (path / "script.txt").write_text("В этом году всё хорошо.\n", encoding="utf-8")
    assert main(["synth", article_id(path)]) == 1
    assert "summary" in capsys.readouterr().err


def test_unknown_dir_exits(lib):
    with pytest.raises(SystemExit):
        main(["lint", "нет-такого"])


def test_speed_out_of_range(lib):
    with pytest.raises(SystemExit):
        main(["synth", "x", "--speed", "3"])


def test_normalize_prints_speakable_text(tmp_path, capsys):
    src = tmp_path / "note.md"
    src.write_text("**Go 1.26** вышел 8 октября.\n", encoding="utf-8")
    assert main(["normalize", str(src)]) == 0
    assert capsys.readouterr().out == "Го один точка двадцать шесть вышел восьмого октября.\n"


def test_say_name_tells_digests_apart(lib):
    from narrator.cli import say_name
    from narrator.library import Entry, create_entry

    digest = create_entry(lib, Entry(title="Дайджест", added="2026-10-08"), "digest")
    (digest / "narration.md").write_text("Текст.", encoding="utf-8")
    assert say_name(digest / "narration.md") == f"{article_id(digest)}-narration"
    assert say_name(lib / "заметка.txt").startswith(lib.name.lower()[:3])


def test_say_reports_missing_file(tmp_path, capsys):
    assert main(["say", str(tmp_path / "нет.md"), "--engine", "silero"]) == 1
    assert "нет файла" in capsys.readouterr().err
