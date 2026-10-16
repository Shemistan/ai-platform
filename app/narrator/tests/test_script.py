from narrator.script import ACUTE, has_errors, lint, normalize, parse


def codes(text: str) -> list[str]:
    return [i.code for i in lint(text)]


def test_parse_splits_paragraphs_and_headings():
    text = "# Введение\n\nПервая строка\nвторая строка.\n\n\nЕщё абзац.\n"
    blocks = parse(text)
    assert [(b.kind, b.text, b.line) for b in blocks] == [
        ("heading", "Введение", 1),
        ("para", "Первая строка вторая строка.", 3),
        ("para", "Ещё абзац.", 7),
    ]


def test_clean_text_passes():
    text = (
        "# Часть первая. Раздел.\n\n"
        "Модель Опен Эй Ай вышла двенадцатого сентября. Зáмок стоит, всё хорошо!\n"
    )
    assert lint(text) == []


def test_normalize_turns_latin_stressed_vowels_into_cyrillic():
    # «á» здесь — латинская буква с акутом, какой её набирают на клавиатуре.
    assert normalize("зáмок") == "за" + ACUTE + "мок"
    assert normalize("за" + ACUTE + "мок") == "за" + ACUTE + "мок"
    assert normalize("ёжик и йогурт") == "ёжик и йогурт"


def test_digits_latin_plus_are_errors():
    assert codes("В 2026 году рост сорок два процента.") == ["DIGIT"]
    assert codes("Вышел новый Kubernetes.") == ["LATIN"]
    assert codes("Кв+ен вышел вчера.") == ["PLUS"]


def test_symbols_are_errors_once_per_char():
    issues = lint("Смотри (ниже) и 50% / всё.")
    assert [i.code for i in issues] == ["CHAR", "CHAR", "DIGIT", "CHAR", "CHAR"]
    assert has_errors(issues)


def test_abbreviations():
    assert codes("Это т.е. пример.") == ["ABBR"]
    assert codes("Три млн человек.") == ["ABBR"]
    assert codes("Пять тысяч человек и др. люди.") == ["ABBR"]
    # «г» внутри слова и «тыс» как часть слова — не сокращения.
    assert codes("Много тысяч, строго.") == []


def test_warnings_do_not_block():
    issues = lint("Модель Оупен-Эй-Ай и ИИ.")
    assert [i.code for i in issues] == ["HYPHEN_CHAIN", "CAPS"]
    assert not has_errors(issues)
    assert codes("Кто-то из-за угла.") == []


def test_long_sentence_warning():
    long = "слово " * 60
    issues = lint(long.strip() + ".")
    assert [i.code for i in issues] == ["LONG_SENTENCE"]


def test_empty_script_is_error():
    assert codes("\n\n") == ["EMPTY"]


def test_heading_marker_is_not_a_symbol_error():
    assert lint("# Часть первая. Итоги.\n") == []
    issues = lint("# Итоги года 2026\n")
    assert [(i.code, i.col) for i in issues] == [("DIGIT", 14)]


def test_short_block_warning():
    issues = lint("# Нейросети\n\nПереходим к нейросетям.\n")
    assert [(i.code, i.line) for i in issues] == [("SHORT_BLOCK", 1)]
