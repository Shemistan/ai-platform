import pytest

from narrator import script
from narrator.normalize import changed, normalize, speakable
from narrator.script import ACUTE
from narrator.verify import compare


def plain(text: str) -> str:
    return text.replace(ACUTE, "")


@pytest.mark.parametrize(
    ("raw", "spoken"),
    [
        # термины из словаря, с заглавной буквы у имён
        ("GitHub и Go", "Гитхаб и Го"),
        ("На Hacker News спорят про DeepSeek", "На Хакер ньюс спорят про Дипсик"),
        ("Let's Encrypt и Hetzner", "Летс энкрипт и Хетцнер"),
        ("В X5 и 1С", "В икс пять и один эс"),
        ("API по HTTP, ответ в JSON", "эй пи ай по эйч ти ти пи, ответ в джейсон"),
        ("PostgreSQL 17.7", "Постгрес семнадцать точка семь"),
        ("C++ и CI/CD", "си плюс плюс и си ай си ди"),
        # даты и годы
        ("вышел 8 октября", "вышел восьмого октября"),
        ("дайджест за 8 октября", "дайджест за восьмое октября"),
        ("в 2028 году", "в две тысячи двадцать восьмом году"),
        ("с 2020 года", "с две тысячи двадцатого года"),
        ("к 2030 году", "к две тысячи тридцатому году"),
        ("дата 08.10.2026", "дата восьмое октября две тысячи двадцать шестого года"),
        ("в XXI веке", "в двадцать первом веке"),
        ("в 90-е годы", "в девяностые годы"),
        ("1-й релиз и 2-го числа", "первый релиз и второго числа"),
        ("5-летний план", "пятилетний план"),
        # падеж по предлогу, род по существительному
        ("с 90 до 64 дней", "с девяносто до шестидесяти четырёх дней"),
        ("больше 400 комментариев", "больше четырёхсот комментариев"),
        ("2 недели и 2 дня", "две недели и два дня"),
        ("21 день, 1 минута", "двадцать один день, одна минута"),
        ("1500 строк", "тысяча пятьсот строк"),
        # единицы
        ("рост на 42%", "рост на сорок два процента"),
        ("до 50%", "до пятидесяти процентов"),
        ("стоит $1", "стоит один доллар"),
        ("5 ГБ и 200 мс", "пять гигабайт и двести миллисекунд"),
        ("3,5 млн запросов", "три с половиной миллиона запросов"),
        ("0,25 секунды", "ноль целых двадцать пять сотых секунды"),
        ("10k запросов", "десять тысяч запросов"),
        ("в 10x быстрее", "в десять раз быстрее"),
        ("экран 1920x1080", "экран тысяча девятьсот двадцать на тысяча восемьдесят"),
        ("в 12:30", "в двенадцать тридцать"),
        ("от −5 градусов", "от минус пяти градусов"),
        ("10–15 минут", "десять — пятнадцать минут"),
        ("код 007", "код ноль ноль семь"),
        # сокращения и аббревиатуры
        ("т.е. и т.д.", "то есть и так далее"),
        ("ИИ в США", "искусственный интеллект в сэ шэ а"),
        ("3D и 5G, чип M1", "три ди и пять джи, чип эм один"),
        (
            "закрыта CVE-2026-1234",
            "закрыта си ви и две тысячи двадцать шесть — тысяча двести тридцать четыре",
        ),
        # разметка, ссылки, символы
        ("**Важно**: `код` и _курсив_", "Важно: код и курсив"),
        ("см. [доки](https://go.dev) и https://x.test/a", "смотри доки и"),
        ("- пункт списка (в скобках)!", "пункт списка, в скобках!"),
        ("A & B = C", "эй и би равно си"),
    ],
)
def test_speakable(raw, spoken):
    got = plain(speakable(raw))
    if raw[0].isdigit():  # строка с числа начинается с заглавной буквы
        assert got[0].isupper()
        got = got[0].lower() + got[1:]
    assert got == spoken


def test_links_leave_no_debris():
    assert speakable("Рост за две недели, см. https://go.dev/doc.") == "Рост за две недели."
    assert speakable("Подробнее: https://x.test/a. Дальше текст.") == "Дальше текст."
    assert speakable("Код лежит на www.site.ru, и всё.") == "Код лежит на, и всё."


def test_unknown_english_words_are_transcribed():
    # По словарю произношений, с ударением; безударные гласные — по написанию.
    assert speakable("OpenTelemetry") == f"О{ACUTE}упен теле{ACUTE}метри"
    assert plain(speakable("kubectl apply")) == "кубектл аплай"
    # Слова нет в словаре — по правилам чтения; без гласных — по буквам.
    assert speakable("Zed") == "Зед"
    assert speakable("gRPC") == "джи ар пи си"
    assert plain(speakable("base time code")) == "бейс тайм коуд"
    assert speakable("SRE") == "эс ар и"


def test_result_passes_lint():
    raw = (
        "## Релизы (8.10)\n\nGo 1.26.3 закрывает 15 CVE — см. https://go.dev/doc. "
        "Рост 42% за 2 недели, $1 → 0,5¢; т.е. ~10x!\n\n* PostgreSQL 17.7 & Redis 8\n"
    )
    issues = script.lint(normalize(raw))
    assert not script.has_errors(issues), [str(i) for i in issues]


def test_clean_script_is_untouched():
    clean = (
        "# Часть первая. Нейросети.\n\nСтарый за́мок стоит на горе — и это\n"
        'уже чистый текст: «цитата», "кавычки", многоточие… Всё ок. Мы ели рис.\n'
    )
    assert normalize(clean) == clean
    assert changed(clean, normalize(clean)) == 0
    assert normalize(normalize("Go 1.26 и 42%")) == normalize("Go 1.26 и 42%")


def test_line_numbers_are_kept():
    raw = (
        "# Итоги\n\nGitHub закрыл лазейку.\nВторая строка абзаца про Go.\n\n---\n\n"
        "Ещё 2 абзаца текста.\n"
    )
    out = normalize(raw)
    assert len(out.splitlines()) == len(raw.splitlines())
    blocks = script.parse(out)
    # Заголовок из одного слова ушёл в начало следующего абзаца — отдельным предложением.
    assert [b.line for b in blocks] == [3, 8]
    assert blocks[0].text == "Итоги. Гитхаб закрыл лазейку. Вторая строка абзаца про Го."
    assert blocks[1].text == "Ещё два абзаца текста."
    assert changed(raw, out) == 5


def test_short_last_block_joins_previous():
    out = normalize("Первый абзац этого текста.\n\nДо завтра!\n")
    assert [b.text for b in script.parse(out)] == ["Первый абзац этого текста. До завтра!"]
    assert normalize("Привет.\n") == "Привет.\n"  # единственный блок остаётся как есть


def test_markdown_headings_become_script_headings():
    blocks = script.parse(normalize("### Что нового в Go 1.26\n\nТекст первого абзаца тут.\n"))
    assert blocks[0].kind == "heading"
    assert blocks[0].text == "Что нового в Го один точка двадцать шесть"


def test_verify_reads_latin_the_same_way():
    # Whisper пишет термины латиницей и числа цифрами — это не искажение озвучки.
    source = speakable("На Hacker News 400 комментариев про GitHub и API")
    score, missed = compare(source, "На Hacker News 400 комментариев про GitHub и API.")
    assert (score, missed) == (1.0, [])
