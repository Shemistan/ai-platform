"""Забрать материалы источника: RSS/Atom, страница, GitHub, go.dev, YouTube, t.me,
базы уязвимостей Go и GitHub.

Сеть — только через `get(url) -> str`: в коде это `http_get`, в тестах —
словарь с заготовленными ответами. Разбор — `xml.etree` и `lxml.html`.
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urljoin, urlsplit

from narrator.news.sources import Source

Getter = Callable[[str], str]
TIMEOUT = 30
USER_AGENT = "Mozilla/5.0 (Macintosh) narrator-news/1.0 (+personal digest)"
SUMMARY_LIMIT = 600
# Так отвечает прокси sandbox агента на закрытый домен: сбоя источника тут нет.
SANDBOX_BLOCK = "Tunnel connection failed: 403"


def is_web(url: str) -> bool:
    """Ссылка http(s) с хостом: иначе (javascript:, data:, file:) в базу её не пускаем."""
    parts = urlsplit(url.strip())
    return parts.scheme.lower() in ("http", "https") and bool(parts.netloc)


class FetchError(RuntimeError):
    """Источник не отдал материалы: сеть, HTTP или непонятный формат."""


@dataclass
class Item:
    url: str
    title: str
    published: str | None = None  # ISO 8601 в UTC; None — дата неизвестна
    summary: str = ""


MAX_RESPONSE_BYTES = 10 * 1024 * 1024  # больше лента или страница не бывает — читать не будем


def http_get(url: str, attempts: int = 2) -> str:
    """Тело ответа; оборванный ответ (IncompleteRead) — ещё одна попытка."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(MAX_RESPONSE_BYTES + 1)  # лента не раздует процесс (SEC-009)
            if len(body) > MAX_RESPONSE_BYTES:
                raise FetchError(f"{url}: ответ больше {MAX_RESPONSE_BYTES // 2**20} МБ")
            return body.decode(charset, errors="replace")
    except urllib.error.HTTPError as e:
        raise FetchError(f"{url}: HTTP {e.code}") from e
    except http.client.IncompleteRead as e:
        if attempts > 1:
            return http_get(url, attempts - 1)
        raise FetchError(f"{url}: ответ оборвался, получено {len(e.partial)} байт") from e
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        raise FetchError(f"{url}: {e}") from e


def clean(text: str | None, limit: int = SUMMARY_LIMIT) -> str:
    """Текст без HTML-тегов и лишних пробелов, не длиннее limit."""
    if not text:
        return ""
    if "<" in text:
        import lxml.html

        try:
            text = lxml.html.fromstring(f"<div>{text}</div>").text_content()
        except (ValueError, TypeError):
            text = re.sub(r"<[^>]+>", " ", text)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def to_iso(raw: str | None) -> str | None:
    """RFC 822 или ISO 8601 → ISO 8601 в UTC; непонятное — None."""
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = parsedate_to_datetime(raw)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, *names: str) -> ET.Element | None:
    for name in names:
        for c in el:
            if _local(c.tag) == name:
                return c
    return None


def _text(el: ET.Element, *names: str) -> str | None:
    c = _child(el, *names)
    return c.text if c is not None and c.text else None


def _atom_link(entry: ET.Element) -> str | None:
    links = [c for c in entry if _local(c.tag) == "link"]
    for link in links:
        if link.get("rel", "alternate") == "alternate" and link.get("href"):
            return link.get("href")
    return links[0].get("href") if links else None


def _media_description(entry: ET.Element) -> str | None:
    group = _child(entry, "group")
    return _text(group, "description") if group is not None else None


def parse_feed(text: str, base: str = "") -> list[Item]:
    """RSS 2.0, Atom и RSS 1.0 (RDF) → материалы в порядке ленты."""
    try:
        root = ET.fromstring(text.lstrip("﻿ \n\r\t").encode("utf-8"))
    except ET.ParseError as e:
        raise FetchError(f"лента не разобралась: {e}") from e
    items: list[Item] = []
    for el in root.iter():
        name = _local(el.tag)
        if name == "item":  # RSS 2.0 и RDF
            url = _text(el, "link") or _text(el, "guid")
            date = _text(el, "pubDate", "date", "published", "updated")
            summary = _text(el, "description", "encoded")
        elif name == "entry":  # Atom
            url = _atom_link(el)
            date = _text(el, "published", "updated")
            summary = _text(el, "summary", "content") or _media_description(el)
        else:
            continue
        title = clean(_text(el, "title"), 300)
        if not url or not title:
            continue
        url = urljoin(base, url.strip())
        if not is_web(url):  # javascript:… из чужой ленты не должен стать ссылкой на странице
            continue
        items.append(Item(url, title, to_iso(date), clean(summary)))
    if not items and _local(root.tag) not in ("rss", "feed", "RDF"):
        raise FetchError(f"это не лента: корневой тег <{_local(root.tag)}>")
    return items


_FEED_TYPES = ("application/rss+xml", "application/atom+xml", "application/feed+xml")


def find_feed(html: str, base: str) -> str | None:
    """Адрес ленты из <link rel="alternate" type="application/rss+xml">."""
    import lxml.html

    try:
        doc = lxml.html.fromstring(html)
    except (ValueError, TypeError):
        return None
    for link in doc.iter("link"):
        rel = (link.get("rel") or "").lower().split()
        if "alternate" in rel and (link.get("type") or "").lower() in _FEED_TYPES:
            href = link.get("href")
            if href:
                return urljoin(base, href)
    return None


def page_links(html: str, base: str, match: str) -> list[Item]:
    """Ссылки страницы, чей путь начинается с match и длиннее него: статьи раздела."""
    import lxml.html

    try:
        doc = lxml.html.fromstring(html)
    except (ValueError, TypeError) as e:
        raise FetchError(f"страница не разобралась: {e}") from e
    host = urlsplit(base).netloc
    seen: dict[str, Item] = {}
    for a in doc.iter("a"):
        href = a.get("href")
        if not href:
            continue
        url = urljoin(base, href).split("#")[0]
        if not is_web(url):
            continue
        parts = urlsplit(url)
        path = parts.path
        if parts.netloc != host or not path.startswith(match):
            continue
        if len(path.rstrip("/")) <= len(match.rstrip("/")):
            continue
        title = clean(a.text_content(), 300)
        if len(title.split()) < 3:  # «Read more», «Next», номера страниц
            continue
        known = seen.get(url)
        if known is None or len(title) > len(known.title):
            seen[url] = Item(url, title)
    return list(seen.values())


def fetch_page(src: Source, get: Getter) -> list[Item]:
    html = get(src.url)
    feed = find_feed(html, src.url)
    if feed:
        try:
            items = parse_feed(get(feed), feed)
        except FetchError:
            items = []
        if items:
            return items
    return page_links(html, src.url, src.match or "/")


def fetch_github(src: Source, get: Getter) -> list[Item]:
    kind = "tags" if src.tags else "releases"
    feed = f"https://github.com/{src.repo}/{kind}.atom"
    items = parse_feed(get(feed), feed)
    if src.exclude:
        skip = re.compile(src.exclude)
        items = [i for i in items if not skip.search(i.title)]
    for i in items:
        if not i.title.lower().startswith(src.repo.split("/")[-1].lower()):
            i.title = f"{src.name}: {i.title}"
    return items


def fetch_godl(src: Source, get: Getter) -> list[Item]:
    try:
        versions = json.loads(get("https://go.dev/dl/?mode=json&include=all"))
    except ValueError as e:
        raise FetchError(f"go.dev/dl: не JSON: {e}") from e
    out = []
    for v in versions:
        name = v.get("version", "")
        if not v.get("stable") or not name.startswith("go"):
            continue
        number = name[2:]
        out.append(
            Item(
                url=f"https://go.dev/doc/devel/release#{name}",
                title=f"Вышел Go {number}",
                summary=f"Стабильная версия Go {number} на go.dev/dl.",
            )
        )
    return out


GOVULN = "https://vuln.go.dev"
GOVULN_LIMIT = 30  # столько последних изменённых записей открываем за сбор


def _json(get: Getter, url: str):
    try:
        return json.loads(get(url))
    except ValueError as e:
        raise FetchError(f"{url}: не JSON: {e}") from e


def fetch_govuln(src: Source, get: Getter) -> list[Item]:
    """Go Vulnerability Database: последние изменённые записи, проверенные командой Go."""
    index = _json(get, f"{GOVULN}/index/vulns.json")
    latest = sorted(index, key=lambda v: v.get("modified", ""), reverse=True)[:GOVULN_LIMIT]
    out = []
    for entry in latest:
        osv = _json(get, f"{GOVULN}/ID/{entry['id']}.json")
        extra = osv.get("database_specific") or {}
        if extra.get("review_status", "REVIEWED") != "REVIEWED":
            continue
        modules = sorted(
            {a["package"]["name"] for a in osv.get("affected", []) if a.get("package")}
        )
        aliases = ", ".join(osv.get("aliases", []))
        text = osv.get("details", "")
        if modules:
            text = f"Модули: {', '.join(modules)}. {text}"
        if aliases:
            text = f"{text} Также: {aliases}."
        out.append(
            Item(
                url=extra.get("url") or f"https://pkg.go.dev/vuln/{osv['id']}",
                title=f"{osv['id']}: {osv.get('summary') or ', '.join(modules)}",
                published=to_iso(osv.get("published") or osv.get("modified")),
                summary=clean(text),
            )
        )
    return out


SEVERITY = ("unknown", "low", "medium", "high", "critical")


def fetch_ghsa(src: Source, get: Getter) -> list[Item]:
    """GitHub Advisory Database по экосистеме; серьёзнее min_severity, с EPSS."""
    url = f"https://api.github.com/advisories?ecosystem={src.ecosystem}&per_page=30"
    if src.packages:  # без списка в экосистеме Go — сплошь конечные приложения
        url += "&affects=" + quote(",".join(src.packages), safe=",/")
    floor = SEVERITY.index(src.min_severity) if src.min_severity in SEVERITY else 0
    out = []
    for adv in _json(get, url):
        severity = adv.get("severity") or "unknown"
        if severity in SEVERITY and SEVERITY.index(severity) < floor:
            continue
        packages = sorted(
            {v["package"]["name"] for v in adv.get("vulnerabilities") or [] if v.get("package")}
        )
        ids = adv["ghsa_id"] + (f" / {adv['cve_id']}" if adv.get("cve_id") else "")
        epss = adv.get("epss") or {}
        notes = [f"Серьёзность: {severity}"]
        if epss.get("percentage") is not None:
            notes.append(f"EPSS {epss['percentage']}%, перцентиль {epss.get('percentile')}")
        if packages:
            notes.append("пакеты: " + ", ".join(packages))
        out.append(
            Item(
                url=adv["html_url"],
                title=f"{ids}: {adv.get('summary', '')}",
                published=to_iso(adv.get("published_at")),
                summary=clean("; ".join(notes) + ". " + (adv.get("description") or "")),
            )
        )
    return out


_CHANNEL_ID = re.compile(r'(?:"channelId":"|"externalId":"|/channel/)(UC[\w-]{22})')


def youtube_channel_id(handle: str, get: Getter) -> str:
    page = get(f"https://www.youtube.com/@{quote(handle)}")
    found = _CHANNEL_ID.search(page)
    if not found:
        raise FetchError(f"YouTube @{handle}: channel_id не найден на странице")
    return found.group(1)


def fetch_youtube(src: Source, get: Getter, memo: dict[str, str]) -> list[Item]:
    """Лента канала; channel_id из конфига, из памяти или со страницы @handle."""
    key = f"youtube:{src.handle}"
    channel_id = src.channel_id or memo.get(key)
    if not channel_id:
        try:
            channel_id = youtube_channel_id(src.handle or "", get)
        except FetchError as e:
            if "HTTP 404" in str(e):
                raise FetchError(
                    f"YouTube @{src.handle}: канала с таким handle нет — проверь его"
                    " или впиши channel_id в конфиг"
                ) from e
            raise
    memo[key] = channel_id
    # Лента канала с конца 2025 года то и дело отвечает 404/500 на живые каналы.
    # Тогда берём ленту плейлиста загрузок: UC… → UULF… (без шортсов) или UU….
    base = "https://www.youtube.com/feeds/videos.xml"
    tail = channel_id[2:] if channel_id.startswith("UC") else ""
    urls = [f"{base}?channel_id={channel_id}"]
    urls += [f"{base}?playlist_id={p}{tail}" for p in ("UULF", "UU") if tail]
    first: FetchError | None = None
    for url in urls:
        try:
            return parse_feed(get(url), url)
        except FetchError as e:
            first = first or e
    assert first is not None
    raise first


def fetch_telegram(src: Source, get: Getter) -> list[Item]:
    """Посты публичного канала из веб-превью t.me/s/<канал>."""
    import lxml.html

    base = f"https://t.me/s/{src.channel}"
    try:
        doc = lxml.html.fromstring(get(base))
    except (ValueError, TypeError) as e:
        raise FetchError(f"{base}: не разобралось: {e}") from e
    out = []
    for msg in doc.xpath('//div[contains(@class, "tgme_widget_message ")][@data-post]'):
        body = msg.xpath('.//div[contains(@class, "tgme_widget_message_text")]')
        text = clean(body[0].text_content() if body else "", 1200)
        if not text:
            continue
        stamp = msg.xpath(".//time/@datetime")
        first = text.split(". ")[0]
        title = first if len(first) <= 140 else first[:139].rstrip() + "…"
        out.append(
            Item(
                url=f"https://t.me/{msg.get('data-post')}",
                title=title,
                published=to_iso(stamp[0]) if stamp else None,
                summary=clean(text),
            )
        )
    if not out and not doc.xpath('//*[contains(@class, "tgme_channel_info")]'):
        raise FetchError(f"{base}: превью канала пустое — это чат или закрытый канал?")
    return out


def fetch(src: Source, get: Getter = http_get, memo: dict[str, str] | None = None) -> list[Item]:
    """Материалы источника; FetchError — если источник не ответил или непонятен."""
    memo = {} if memo is None else memo
    if src.kind == "feed":
        return parse_feed(get(src.feed or ""), src.feed or "")
    if src.kind == "page":
        return fetch_page(src, get)
    if src.kind == "github":
        return fetch_github(src, get)
    if src.kind == "godl":
        return fetch_godl(src, get)
    if src.kind == "youtube":
        return fetch_youtube(src, get, memo)
    if src.kind == "telegram":
        return fetch_telegram(src, get)
    if src.kind == "govuln":
        return fetch_govuln(src, get)
    if src.kind == "ghsa":
        return fetch_ghsa(src, get)
    raise FetchError(f"{src.id}: неизвестный kind={src.kind}")
