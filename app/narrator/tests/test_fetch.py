import pytest

from narrator import fetch as fetch_mod
from narrator.fetch import FetchError, LocalAddressError, Response, check_url, download, extract

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


# --- SSRF: локальные и внутренние адреса (SEC-005) ---------------------------

LOCAL = {
    "localhost": ["127.0.0.1", "::1"],
    "router.test": ["192.168.1.1"],
    "mixed.test": ["93.184.216.34", "10.0.0.5"],  # хоть один внутренний — отказ
    "blog.test": ["93.184.216.34"],
}


def fake_resolve(host, port):
    return LOCAL.get(host, [host])  # IP-литерал резолвится сам в себя, без сети


def no_network(url):
    raise AssertionError(f"сетевой запрос к {url}")


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://0.0.0.0:8780/api/config",
        "http://[::ffff:127.0.0.1]/",
        "http://router.test/admin",
        "http://mixed.test/",
        "http://224.0.0.1/",
    ],
)
def test_local_addresses_refused_without_request(url):
    with pytest.raises(LocalAddressError, match="локальный адрес"):
        download(url, get=no_network, resolve=fake_resolve)


def test_real_resolver_refuses_loopback_literals():
    for url in ("http://127.0.0.1:8780/", "http://[::1]/", "http://localhost/"):
        with pytest.raises(LocalAddressError):
            check_url(url)


def test_non_http_scheme_refused():
    with pytest.raises(FetchError, match="https://"):
        download("file:///etc/passwd", get=no_network, resolve=fake_resolve)


def test_redirect_to_loopback_refused():
    calls = []

    def get(url):
        calls.append(url)
        return Response(302, "http://127.0.0.1:8780/api/config", b"")

    with pytest.raises(LocalAddressError):
        download("https://blog.test/a", get=get, resolve=fake_resolve)
    assert calls == ["https://blog.test/a"]  # на loopback запроса не было


def test_redirect_follow_and_limit():
    pages = {
        "https://blog.test/a": Response(301, "/b", b""),
        "https://blog.test/b": Response(200, None, HTML.encode()),
    }
    html = download("https://blog.test/a", get=pages.__getitem__, resolve=fake_resolve)
    assert "Синтез речи" in html

    loop = Response(302, "https://blog.test/a", b"")
    with pytest.raises(FetchError, match="редиректов"):
        download("https://blog.test/a", get=lambda u: loop, resolve=fake_resolve)


def test_pinned_connection_rechecks_address(monkeypatch):
    # DNS rebinding: при соединении адрес проверяется снова, сокет к loopback не открывается.
    opened = []
    monkeypatch.setattr(fetch_mod.socket, "create_connection", lambda *a, **k: opened.append(a))
    create = fetch_mod._pinned(lambda host, port: ["127.0.0.1"])
    with pytest.raises(LocalAddressError):
        create(("blog.test", 443))
    assert opened == []
