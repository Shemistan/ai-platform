from narrator.script import ACUTE
from narrator.stress import acute_to_plus, merge, plus_to_acute, strip_marks


def test_acute_plus_roundtrip():
    assert acute_to_plus("за" + ACUTE + "мок") == "з+амок"
    assert acute_to_plus("зáмок") == "з+амок"  # латинская «á» тоже
    assert plus_to_acute("з+амок и зам+ок") == "за" + ACUTE + "мок и замо" + ACUTE + "к"
    assert strip_marks("з+амок за" + ACUTE + "мок") == "замок замок"


def test_merge_keeps_ruaccent_where_no_manual_stress():
    src = "Старый замок закрыт."
    acc = "Ст+арый зам+ок закр+ыт."
    assert merge(src, acc) == acc


def test_merge_manual_acute_wins():
    src = "Старый за" + ACUTE + "мок закрыт."
    acc = "Ст+арый зам+ок закр+ыт."
    assert merge(src, acc) == "Ст+арый з+амок закр+ыт."


def test_merge_applies_fixes_for_slang():
    assert merge("Роль продактов растёт.", "Р+оль прод+актов раст+ёт.") == (
        "Р+оль пр+одактов раст+ёт."
    )
    assert merge("Продакт решил.", "Прод+акт реш+ил.") == "Пр+одакт реш+ил."


def test_merge_manual_beats_fix():
    src = "продa" + ACUTE + "ктов"
    assert merge(src, "прод+актов") == "прод+актов"


def test_merge_word_count_mismatch_still_fixes():
    # RUAccent склеил или разбил слова — ручные ударения не накладываем, поправки да.
    assert merge("из-за продактов", "из за прод+актов") == "из за пр+одактов"


def test_manual_marks_keeps_only_manual_and_fixes():
    from narrator.stress import manual_marks

    src = "Старый за" + ACUTE + "мок, роль продактов."
    assert manual_marks(src) == "Старый з+амок, роль пр+одактов."
    assert manual_marks("Обычный текст.") == "Обычный текст."
