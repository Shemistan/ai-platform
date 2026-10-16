import json

from narrator.script import ACUTE
from narrator.synth import MANIFEST
from narrator.verify import check, compare, report, words


def test_words_normalizes_case_yo_and_stress():
    assert words("Всё за" + ACUTE + "мок, Ёжик!") == ["все", "замок", "ежик"]


def test_compare_perfect_and_missed():
    assert compare("Привет, мир.", "привет мир") == (1.0, [])
    score, missed = compare("Кубернетес и Постгрес вышли.", "Кубернетес и способ гряз вышли.")
    assert missed == ["постгрес"]
    assert score == 0.75


def test_compare_digits_in_transcript():
    score, missed = compare("Сорок два процента.", "42 процента")
    assert (score, missed) == (1.0, [])


def test_check_caches_transcripts_and_reports(tmp_path):
    manifest = [
        {"line": 1, "kind": "para", "text": "Хороший абзац.", "clip": "a.wav"},
        {"line": 3, "kind": "para", "text": "Опен Эй Ай выпустила модель.", "clip": "b.wav"},
    ]
    (tmp_path / MANIFEST).write_text(json.dumps(manifest), encoding="utf-8")
    heard = {"a.wav": "Хороший абзац.", "b.wav": "А пенаяй выпустила модель."}
    calls = []

    def fake(path):
        calls.append(path.name)
        return heard[path.name]

    results = check(tmp_path, fake)
    assert [r.line for r in results] == [1, 3]
    assert check(tmp_path, fake) and calls == ["a.wav", "b.wav"]  # второй раз из кэша

    text = report(results, threshold=0.85)
    assert "блоков ниже 85%: 1 из 2" in text
    assert "## Строка 3" in text
    assert "не найдено: опен, эй, ай" in text
    assert "Строка 1" not in text


def test_compare_ordinal_headings_written_as_digits():
    for src, heard in [
        ("Часть пятая. Метрики этого не видят.", "Часть 5. Метрики этого не видят."),
        ("Часть четырнадцатая. Вопросы для спора.", "Часть 14. Вопросы для спора."),
        ("Часть пятнадцатая. Заключение.", "Часть 15. Заключение."),
        ("Часть третья. Нейросети.", "Часть 3. Нейросети."),
        ("в мае две тысячи двадцать шестого года", "в мае 2026 года"),
    ]:
        assert compare(src, heard) == (1.0, []), src


def test_compare_number_keys_do_not_hide_real_errors():
    _, missed = compare("Тесты необходимы, но их недостаточно.", "тесто необходимое, но их мало")
    assert missed == ["тесты", "необходимы", "недостаточно"]
