"""Скачивание статьи и извлечение основного текста (trafilatura), дословно.

Ссылку присылает человек или чужая страница (через платформу), поэтому
скачивание не ходит на локальные и внутренние адреса (SSRF): хост
резолвится, и если хоть один адрес loopback, частный, link-local и т. п. —
отказ без запроса. Соединение открывается на уже проверенный адрес (DNS
rebinding между проверкой и запросом не проходит), редиректы — вручную, с
той же проверкой на каждом шаге; размер ответа и время ограничены.
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

TIMEOUT = 30
MAX_BYTES = 10 * 1024 * 1024
MAX_REDIRECTS = 5
USER_AGENT = "Mozilla/5.0 (Macintosh) narrator/1.0 (+personal reader)"
LOCAL_ADDRESS = "Ссылка ведёт на локальный адрес — не загружаю"

# resolve(host, port) -> [адрес, …]; в тестах — заглушка без сети.
Resolver = Callable[[str, int], list[str]]


@dataclass
class Article:
    url: str
    title: str | None
    author: str | None
    published: str | None
    text: str

    def to_markdown(self) -> str:
        head = [f"# {self.title or 'Без заголовка'}", "", f"- ссылка: {self.url}"]
        if self.author:
            head.append(f"- автор: {self.author}")
        if self.published:
            head.append(f"- дата: {self.published}")
        return "\n".join(head) + "\n\n" + self.text.strip() + "\n"


class FetchError(RuntimeError):
    pass


def extract(html: str, url: str) -> Article:
    import trafilatura

    doc = trafilatura.bare_extraction(
        html,
        url=url,
        output_format="python",
        include_comments=False,
        include_tables=True,
        include_formatting=True,
        include_links=False,
        with_metadata=True,
    )
    if doc is None or not (doc.text or "").strip():
        raise FetchError(f"{url}: не удалось выделить текст статьи")
    return Article(
        url=url,
        title=doc.title,
        author=doc.author,
        published=doc.date,
        text=doc.text,
    )


class LocalAddressError(FetchError):
    """Ссылка (или редирект) ведёт на loopback, частную сеть, метаданные облака и т. п."""


def system_resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as e:
        raise FetchError(f"{host}: адрес не найден ({e})") from e
    return list(dict.fromkeys(info[4][0] for info in infos))


def is_public(address: str) -> bool:
    """Адрес из интернета, а не loopback, частная сеть, link-local, multicast и пр."""
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None) or getattr(ip, "sixtofour", None)
    if mapped is not None and not is_public(str(mapped)):
        return False
    bad = (
        ip.is_loopback
        or ip.is_private
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
    return not bad and ip.is_global


def public_addresses(host: str, port: int, resolve: Resolver = system_resolve) -> list[str]:
    """Адреса хоста; хоть один не публичный — LocalAddressError (без сетевого запроса)."""
    if not host:
        raise FetchError("в ссылке нет хоста")
    addresses = resolve(host.strip("[]"), port)
    if not addresses:
        raise FetchError(f"{host}: адрес не найден")
    if not all(is_public(a) for a in addresses):
        raise LocalAddressError(f"{LOCAL_ADDRESS}: {host}")
    return addresses


def check_url(url: str, resolve: Resolver = system_resolve) -> tuple[str, int]:
    """Ссылка http(s) на публичный адрес → (хост, порт); иначе FetchError."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"{url}: ссылка — вида https://…")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as e:
        raise FetchError(f"{url}: неверный порт") from e
    host = parts.hostname or ""
    public_addresses(host, port, resolve)
    return host, port


def _pinned(resolve: Resolver):
    """socket.create_connection, который соединяется только с проверенным адресом."""

    def create(address, timeout=TIMEOUT, source_address=None, *args, **kwargs):
        host, port = address
        error: OSError | None = None
        for ip in public_addresses(host, port, resolve):
            try:
                return socket.create_connection((ip, port), timeout, source_address)
            except OSError as e:
                error = e
        raise error or OSError(f"{host}: не соединиться")

    return create


@dataclass
class Response:
    status: int
    location: str | None
    body: bytes


def http_get(url: str, resolve: Resolver = system_resolve) -> Response:
    """Один GET без редиректов, на проверенный адрес, не больше MAX_BYTES."""
    parts = urlsplit(url)
    host, port = check_url(url, resolve)
    if parts.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port, timeout=TIMEOUT, context=ssl.create_default_context()
        )
    else:
        conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT)
    conn._create_connection = _pinned(resolve)  # type: ignore[attr-defined]
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    try:
        conn.request(
            "GET",
            path,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity", "Accept": "*/*"},
        )
        resp = conn.getresponse()
        length = resp.getheader("Content-Length")
        if length and length.isdigit() and int(length) > MAX_BYTES:
            raise FetchError(f"{url}: страница больше {MAX_BYTES // 2**20} МБ")
        body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise FetchError(f"{url}: страница больше {MAX_BYTES // 2**20} МБ")
        return Response(resp.status, resp.getheader("Location"), body)
    except (OSError, http.client.HTTPException) as e:
        raise FetchError(f"{url}: {e}") from e
    finally:
        conn.close()


Getter = Callable[[str], Response]


def download(url: str, get: Getter | None = None, resolve: Resolver = system_resolve) -> str:
    """HTML страницы; редиректы — вручную, каждый шаг проверяется заново."""
    get = get or (lambda u: http_get(u, resolve))
    for _ in range(MAX_REDIRECTS + 1):
        check_url(url, resolve)  # до запроса: локальный адрес — отказ без сети
        resp = get(url)
        if resp.status in (301, 302, 303, 307, 308) and resp.location:
            url = urljoin(url, resp.location)
            continue
        if resp.status != 200:
            raise FetchError(f"{url}: HTTP {resp.status}")
        from trafilatura.utils import decode_file

        return decode_file(resp.body)
    raise FetchError(f"{url}: больше {MAX_REDIRECTS} редиректов")


def fetch(url: str) -> Article:
    try:
        html = download(url)
    except LocalAddressError:
        raise
    except FetchError as e:
        raise FetchError(
            f"{e} — страница не скачалась (нет сети, домен закрыт sandbox'ом "
            "или сайт отдаёт страницу только браузеру)"
        ) from e
    if not html:
        raise FetchError(f"{url}: страница пустая")
    return extract(html, url)
