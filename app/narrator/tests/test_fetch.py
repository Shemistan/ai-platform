import pytest

from narrator.fetch import FetchError, extract

HTML = """<html><head><title>Синтез речи | Блог</title>
<meta name="author" content="Иван Петров">
<meta property="article:published_time" content="2026-09-01">
</head><body>
<nav>Главная Меню Войти</nav>
<article><h1>Синтез речи</h1>
<p>Первый абзац статьи про синтез речи, в нём достаточно текста, чтобы экстрактор
счёл его основным содержанием страницы, а не мусором навигации.</p>
<p>Второй абзац продолжает мысль и рассказывает о моделях, которые работают
локально на ноутбуке без доступа к сети и без видеокарты.</p>
</article><footer>© 2026 Все права защищены</footer></body></html>"""


def test_extract_main_text_and_metadata():
    a = extract(HTML, "https://blog.test/tts")
    assert "Первый абзац статьи" in a.text
    assert "Второй абзац" in a.text
    assert "Все права" not in a.text
    assert a.author == "Иван Петров"
    md = a.to_markdown()
    assert md.startswith("# Синтез речи")
    assert "- ссылка: https://blog.test/tts" in md


def test_extract_empty_page():
    with pytest.raises(FetchError):
        extract("<html><body></body></html>", "https://blog.test/empty")
