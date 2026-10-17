import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from narrator import telegram
from narrator.cli import main
from narrator.library import Entry
from narrator.news import digest, feeds, sources
from narrator.news.feeds import FetchError, Item
from narrator.news.sources import Source
from narrator.news.store import Store, item_id, normalize_url

NOW = datetime(2026, 9, 23, 6, 0, tzinfo=UTC)

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Go</title>
<item><title>Go 1.26 is released</title><link>https://go.dev/blog/go1.26?utm_source=rss</link>
<pubDate>Tue, 22 Sep 2026 18:00:00 GMT</pubDate>
<description>&lt;p&gt;Today the Go team &lt;b&gt;released&lt;/b&gt;
Go 1.26.&lt;/p&gt;</description></item>
<item><title>Old post</title><link>https://go.dev/blog/old</link>
<pubDate>Mon, 01 Jan 2024 10:00:00 GMT</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
<entry><title>v7.4.1</title><link rel="alternate" href="/redis/redis/releases/tag/7.4.1"/>
<updated>2026-09-22T10:00:00Z</updated>
<media:group><media:description>Security fix</media:description></media:group></entry>
<entry><title>8.0-rc1</title><link href="https://github.com/redis/redis/releases/tag/8.0-rc1"/>
<updated>2026-09-22T11:00:00Z</updated></entry>
</feed>"""

RDF = """<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
 xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>RDF item</title><link>https://x.test/rdf</link><dc:date>2026-09-22T09:00:00+03:00</dc:date></item>
</rdf:RDF>"""

PAGE = """<html><head><title>Blog</title></head><body>
<a href="/blog/">Blog</a>
<a href="/blog/how-we-cut-latency-in-half">How we cut latency in half</a>
<a href="/blog/how-we-cut-latency-in-half#comments">Comments</a>
<a href="/blog/page/2">Next</a>
<a href="https://other.test/blog/external-post-here">External post here today</a>
<a href="/careers/join-our-great-team">Join our great team</a>
</body></html>"""

TELEGRAM = """<html><body><div class="tgme_channel_info">AvitoTech</div>
<div class="tgme_widget_message text_shadow js-widget_message" data-post="avitotech/101">
<div class="tgme_widget_message_text js-message_text">Выложили доклад про шардирование Postgres.
Смотрите запись.</div><time datetime="2026-09-22T12:00:00+00:00">12:00</time></div>
<div class="tgme_widget_message text_shadow js-widget_message" data-post="avitotech/102">
<div class="tgme_widget_message_photo_wrap"></div></div>
</body></html>"""


def src(**kw) -> Source:
    base = dict(
        id="go_blog",
        name="The Go Blog",
        url="https://go.dev/blog/",
        type="official",
        category="stack",
        kind="feed",
        feed="https://go.dev/blog/feed.atom",
        authority=1.0,
    )
    base.update(kw)
    return Source(**base)


def getter(pages: dict[str, str]):
    def get(url: str) -> str:
        if url not in pages:
            raise FetchError(f"{url}: HTTP 404")
        return pages[url]

    return get


# --- источники --------------------------------------------------------------


def test_real_config_is_valid():
    loaded = sources.load()
    ids = {s.id for s in loaded}
    assert {"go_blog", "go_releases", "hackernews", "avitotech_tg"} <= ids
    hn = sources.by_id(loaded)["hackernews"]
    assert hn.secondary and hn.authority == 0.45
    assert sources.by_id(loaded)["fireship"].authority == 0.70
    assert not sources.by_id(loaded)["golang_ru_tg"].enabled


def test_config_errors(tmp_path):
    path = tmp_path / "s.toml"
    path.write_text(
        '[types]\nofficial = 1.0\n[[source]]\nid = "x"\nname = "X"\nurl = "u"\n'
        'type = "official"\ncategory = "stack"\nkind = "feed"\n',
        encoding="utf-8",
    )
    with pytest.raises(sources.ConfigError, match="нужно поле feed"):
        sources.load(path)
    path.write_text(path.read_text() + 'feed = "f"\nbogus = 1\n', encoding="utf-8")
    with pytest.raises(sources.ConfigError, match="неизвестные поля"):
        sources.load(path)


# --- разбор лент ------------------------------------------------------------


def test_parse_rss():
    items = feeds.parse_feed(RSS)
    assert [i.title for i in items] == ["Go 1.26 is released", "Old post"]
    assert items[0].published == "2026-09-22T18:00:00+00:00"
    assert items[0].summary == "Today the Go team released Go 1.26."


def test_parse_atom_and_rdf():
    items = feeds.parse_feed(ATOM, "https://github.com/redis/redis/releases.atom")
    assert items[0].url == "https://github.com/redis/redis/releases/tag/7.4.1"
    assert items[0].summary == "Security fix"
    rdf = feeds.parse_feed(RDF)
    assert rdf[0].published == "2026-09-22T06:00:00+00:00"


def test_feed_drops_non_http_links(tmp_path):
    # Вредная лента: ссылка-скрипт стала бы кликабельной на странице новостей (SEC-003).
    evil = """<?xml version="1.0"?><rss version="2.0"><channel><title>X</title>
<item><title>Click me</title><link>javascript:alert(1)</link></item>
<item><title>Data</title><link>data:text/html,hi</link></item>
<item><title>Fine</title><link>https://x.test/ok</link></item>
</channel></rss>"""
    items = feeds.parse_feed(evil, "https://x.test/feed")
    assert [i.url for i in items] == ["https://x.test/ok"]
    with Store(tmp_path / "news.db") as store:
        bad = [Item("javascript:alert(1)", "Evil evil evil"), Item("JavaScript:x", "Evil two")]
        assert store.add(src(), bad, NOW, days=3, baseline=False) == 0
        assert store.db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0


def test_parse_feed_rejects_html():
    with pytest.raises(FetchError):
        feeds.parse_feed("<html><body>not a feed</body></html>")


def test_github_excludes_prereleases():
    s = src(id="redis_github", name="Redis GitHub", kind="github", repo="redis/redis", exclude="rc")
    items = feeds.fetch(s, getter({"https://github.com/redis/redis/releases.atom": ATOM}))
    assert [i.title for i in items] == ["Redis GitHub: v7.4.1"]


def test_page_uses_feed_if_announced():
    html = '<html><head><link rel="alternate" type="application/rss+xml" href="/feed.xml">'
    s = src(kind="page", url="https://x.test/blog/", match="/blog/")
    get = getter({"https://x.test/blog/": html, "https://x.test/feed.xml": RSS})
    assert len(feeds.fetch(s, get)) == 2


def test_page_links_fallback():
    s = src(kind="page", url="https://x.test/blog/", match="/blog/")
    items = feeds.fetch(s, getter({"https://x.test/blog/": PAGE}))
    assert [(i.url, i.title) for i in items] == [
        ("https://x.test/blog/how-we-cut-latency-in-half", "How we cut latency in half")
    ]


def test_godl_stable_only():
    data = json.dumps(
        [{"version": "go1.26.0", "stable": True}, {"version": "go1.27rc1", "stable": False}]
    )
    s = src(kind="godl")
    items = feeds.fetch(s, getter({"https://go.dev/dl/?mode=json&include=all": data}))
    assert [i.title for i in items] == ["Вышел Go 1.26.0"]


def test_youtube_resolves_and_remembers_channel():
    cid = "UC" + "a" * 22
    feed = f"https://www.youtube.com/feeds/videos.xml?channel_id={cid}"
    pages = {"https://www.youtube.com/@hnasr": f'..."channelId":"{cid}"...', feed: ATOM}
    memo: dict[str, str] = {}
    s = src(kind="youtube", handle="hnasr")
    assert len(feeds.fetch(s, getter(pages), memo)) == 2
    assert memo == {"youtube:hnasr": cid}
    del pages["https://www.youtube.com/@hnasr"]
    assert len(feeds.fetch(s, getter(pages), memo)) == 2


def test_telegram_channel_preview():
    s = src(kind="telegram", channel="avitotech")
    items = feeds.fetch(s, getter({"https://t.me/s/avitotech": TELEGRAM}))
    assert len(items) == 1
    assert items[0].url == "https://t.me/avitotech/101"
    assert items[0].title.startswith("Выложили доклад про шардирование Postgres")
    with pytest.raises(FetchError, match="чат"):
        feeds.fetch(s, getter({"https://t.me/s/avitotech": "<html><body></body></html>"}))


# --- хранилище --------------------------------------------------------------


def test_normalize_url():
    assert (
        normalize_url("HTTP://Go.dev/blog/x/?utm_source=rss&a=1#top") == "https://go.dev/blog/x?a=1"
    )
    assert item_id("https://go.dev/blog/x/") == item_id("https://go.dev/blog/x?utm_medium=y")


def test_store_window_baseline_and_dedup(tmp_path):
    with Store(tmp_path / "news.db") as store:
        s = src()
        items = feeds.parse_feed(RSS)
        assert store.add(s, items, NOW, days=3) == 1  # Old post — в архив
        store.log_run(s.id, NOW, ok=True)
        assert store.add(s, items, NOW, days=3) == 0  # повтор не плодит материалы

        page = src(id="blog", kind="page")
        first = [Item("https://x.test/blog/a", "A a a")]
        assert store.add(page, first, NOW, days=3) == 0  # первый заход — только запомнить
        store.log_run(page.id, NOW, ok=True)
        assert store.add(page, [Item("https://x.test/blog/b", "B b b")], NOW, days=3) == 1
        assert {r["title"] for r in store.pending()} == {"Go 1.26 is released", "B b b"}


# --- оценка -----------------------------------------------------------------


def test_score_and_importance():
    factors = dict(
        global_impact=1,
        stack_relevance=1,
        novelty=1,
        production_impact=1,
        insight=1,
        community_signal=1,
    )
    assert digest.score(factors, 1.0) == 1.0
    assert digest.importance(1.0) == 10
    assert digest.importance(0.0) == 1
    assert digest.importance(0.6) == 7  # 0.6 / 0.1 во float — 5.999…, не должно дать 6
    assert digest.importance(0.59) == 6
    assert digest.final_importance(0.95, secondary=True, confirmed_by=None) == 6
    assert digest.final_importance(0.95, secondary=True, confirmed_by="https://go.dev") == 10


def test_parse_assessments_reports_errors():
    good = dict(
        id="a",
        kind="release",
        event="go-1-26",
        title="Вышел Go",
        why="Новое",
        global_impact=0.8,
        stack_relevance=1,
        novelty=0.8,
        production_impact=0.7,
        insight=0.3,
        community_signal=0.3,
    )
    bad = {**good, "id": "b", "kind": "gossip", "novelty": 2, "event": "Go 1.26"}
    items, skip, errors = digest.parse_assessments({"items": [good, bad], "skip": ["c"]})
    assert [a.id for a in items] == ["a"] and skip == ["c"]
    assert "kind" in errors[0] and "novelty" in errors[0] and "event" in errors[0]


def row(key, event, imp, score):
    return dict(
        id=key,
        event=event,
        importance=imp,
        score=score,
        title_ru=key,
        kind="release",
        why="",
        source="go_blog",
        url="https://x.test/" + key,
        title=key,
        summary="",
        confirmed_by=None,
        factors="{}",
    )


def test_group_and_select():
    rows = [
        row("a", "e1", 10, 0.9),
        row("b", "e1", 6, 0.5),
        row("c", "e2", 8, 0.7),
        row("d", "e3", 5, 0.55),
        row("e", "e4", 4, 0.3),
    ]
    events = digest.group_events(rows)
    assert [e.key for e in events] == ["e1", "e2", "e3", "e4"]
    assert events[0].lead["id"] == "a" and len(events[0].items) == 2
    assert [e.key for e in digest.select(events)] == ["e1", "e2", "e3"]  # добор пятёркой
    assert digest.select([events[3]]) == []


def test_post_html_and_check():
    text = "**Главное за день**\n\n🔒 Уязвимость в [net/http](https://go.dev/x?a=1&b=2) <срочно>"
    out = digest.post_html(text)
    assert out.startswith("<b>Главное за день</b>")
    assert '<a href="https://go.dev/x?a=1&amp;b=2">net/http</a>' in out
    assert "&lt;срочно&gt;" in out
    assert digest.check_post(text) == []
    assert digest.check_post("x" * (digest.POST_LIMIT + 1))
    assert digest.check_post("**незакрыто")
    assert digest.headlines(text) == ["Уязвимость в net/http <срочно>"]


PROMPT = """Create a square 1:1 poster for the cover of a developer news digest.
Main character: the classic Go gopher mascot.
Scene: the gopher swaps an old engine for a new one.
Text on the image:
Title: "Дайджест разработчика"
Date: "23 сентября"
Headlines:
- "Node 20 — всё"
- "PgBouncer: три дыры"
- "json/v2 против nil"
Do not put links, URLs or logos on the image.
"""
POST_TEXT = "**Дайджест**\n\n⚠️ **Node 20** больше нет в Actions — [changelog](https://x)\n"


def filled_prompt(day: date) -> str:
    """Заготовка дня, дописанная как агентом: сцена и три заголовка."""
    from narrator.news import designs

    text = designs.render_prompt(day, "23 сентября")
    text = text.replace(designs.SCENE_HINT, "the gopher dives to catch a falling engine.")
    for n, line in enumerate(("Node 20 — всё", "PgBouncer: три дыры", "json/v2 против nil"), 1):
        text = text.replace(f"<заголовок {n}>", line)
    return text


def test_cover_design_changes_by_day_and_season():
    from narrator.news import cover, designs

    assert len(designs.SEASONS) == 12 and len(designs.STYLES) >= 30
    year = [date(2026, 1, 1) + timedelta(days=n) for n in range(365)]
    assert len({designs.design_for(d).lines for d in year}) == 365  # ни одного повтора
    for month in range(1, 13):
        days = [d for d in year if d.month == month]
        assert len({designs.design_for(d).style for d in days}) == len(days)
        assert len({designs.design_for(d).season for d in days}) == 1
    october, november = designs.design_for(date(2026, 10, 8)), designs.design_for(date(2026, 11, 8))
    assert (october.season, november.season) == ("осень", "поздняя осень")
    assert october.style != november.style  # тот же день, другой месяц — другой дизайн

    day = date(2026, 10, 8)
    draft = designs.render_prompt(day, "8 октября")
    assert 'Date: "8 октября"' in draft and "mid autumn" in draft
    assert "не заполнено" in cover.check_image_prompt(draft, POST_TEXT, day)[0]
    done = filled_prompt(day)
    assert cover.check_image_prompt(done, POST_TEXT, day) == []
    wrapped = done.replace(". Colour palette:", ".\nColour palette:")  # перенос — не подмена
    assert cover.check_image_prompt(wrapped, POST_TEXT, day) == []
    other = filled_prompt(date(2026, 10, 9))
    assert any("оформления дня" in p for p in cover.check_image_prompt(other, POST_TEXT, day))


def test_image_prompt_check():
    from narrator.news import cover

    assert cover.poster_text(PROMPT) == (
        "Дайджест разработчика",
        "23 сентября",
        ["Node 20 — всё", "PgBouncer: три дыры", "json/v2 против nil"],
    )
    assert cover.check_image_prompt(PROMPT, POST_TEXT) == []
    assert "гофер" in cover.check_image_prompt(PROMPT.replace("gopher", "cat"), POST_TEXT)[0]
    old = PROMPT.split("Text on the image")[0]
    assert "нет текста" in cover.check_image_prompt(old, POST_TEXT)[0]
    link = PROMPT.replace("три дыры", "t.me/avitotech")
    assert any("ссыл" in p for p in cover.check_image_prompt(link, POST_TEXT))
    long = PROMPT.replace("три дыры", "закрывает три DoS-уязвимости, две без логина")
    assert any("длинн" in p for p in cover.check_image_prompt(long, POST_TEXT))
    many = PROMPT.replace('- "json/v2 против nil"', '- "а"\n- "б"\n- "в"\n- "г"')
    assert any("заголовков" in p for p in cover.check_image_prompt(many, POST_TEXT))
    same = POST_TEXT + "🐹 **json/v2** против nil\n"
    assert any("post.md" in p for p in cover.check_image_prompt(PROMPT, same))


# --- сквозной сценарий: сбор → оценка → дайджест → отправка -----------------


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path / "library"))
    config = tmp_path / "sources.toml"
    config.write_text(
        """[types]
official = 1.0
aggregator = 0.45
[confirm]
secondary = ["aggregator"]
[[source]]
id = "go_blog"
name = "The Go Blog"
url = "https://go.dev/blog/"
type = "official"
category = "stack"
priority = 5
kind = "feed"
feed = "https://go.dev/blog/feed.atom"
[[source]]
id = "hackernews"
name = "Hacker News"
url = "https://news.ycombinator.com/"
type = "aggregator"
category = "radar"
kind = "feed"
feed = "https://hnrss.org/frontpage"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv(sources.SOURCES_ENV, str(config))
    return tmp_path / "library"


HN = RSS.replace("go.dev/blog/go1.26?utm_source=rss", "news.ycombinator.com/item?id=1").replace(
    "Go 1.26 is released", "Go 1.26 is out (HN)"
)


def fake_fetch(monkeypatch, pages):
    real = feeds.fetch
    monkeypatch.setattr(feeds, "fetch", lambda s, get=None, memo=None: real(s, getter(pages), memo))


def assessment(key, event, **kw):
    base = dict(
        id=key,
        kind="release",
        event=event,
        title="Вышел Go 1.26",
        why="Новый рантайм и генерики",
        global_impact=0.7,
        stack_relevance=1.0,
        novelty=0.9,
        production_impact=0.8,
        insight=0.6,
        community_signal=0.5,
    )
    base.update(kw)
    return base


def test_collect_assess_digest_flow(lib, tmp_path, monkeypatch, capsys):
    # даты лент — «сейчас», иначе окно в 3 дня их отсечёт
    now = datetime.now(UTC)
    stamp = now.strftime("%a, %d %b %Y %H:%M:%S GMT")
    pages = {
        "https://go.dev/blog/feed.atom": RSS.replace("Tue, 22 Sep 2026 18:00:00 GMT", stamp),
        "https://hnrss.org/frontpage": HN.replace("Tue, 22 Sep 2026 18:00:00 GMT", stamp),
    }
    fake_fetch(monkeypatch, pages)
    assert main(["news", "collect"]) == 0
    assert "новых материалов 2" in capsys.readouterr().out

    assert main(["news", "pending"]) == 0
    listing = capsys.readouterr().out
    go_id = item_id("https://go.dev/blog/go1.26")
    hn_id = item_id("https://news.ycombinator.com/item?id=1")
    assert go_id in listing and "2°" in listing

    data = {"items": [assessment(go_id, "go-1-26"), assessment(hn_id, "go-1-26")]}
    file = tmp_path / "a.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    assert main(["news", "assess", str(file)]) == 0
    assert "оценено 2" in capsys.readouterr().out

    assert main(["news", "top"]) == 0
    assert " 9/10" in capsys.readouterr().out

    # одно событие — меньше трёх, но добирать нечем: дайджест из одного
    assert main(["news", "digest"]) == 0
    path = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert path.name == "digest"
    events = (path / "events.md").read_text(encoding="utf-8")
    assert "Вышел Go 1.26" in events and "Hacker News" in events
    meta = Entry.load(path / "meta.json")
    assert meta.kind == "digest" and meta.title.startswith("Дайджест разработчика")

    # повторный digest того же дня берёт те же события, а не пустоту
    assert main(["news", "digest"]) == 0
    capsys.readouterr()

    assert main(["news", "check", str(path)]) == 1
    assert "нет post.md" in capsys.readouterr().out
    (path / "post.md").write_text(
        "**Главное за день**\n🐹 [Вышел Go 1.26](https://go.dev/blog/go1.26)\n", encoding="utf-8"
    )
    (path / "narration.md").write_text("Главное за день. Вышла новая версия языка Go.\n")
    meta.summary = "Вышел Go 1.26."
    meta.save(path / "meta.json")
    assert main(["news", "check", str(path)]) == 1
    out = capsys.readouterr().out
    assert "нет text.md" in out and "image_prompt.md: не заполнено" in out
    day = date.fromisoformat(meta.added)
    (path / "image_prompt.md").write_text(filled_prompt(day), encoding="utf-8")
    assert main(["news", "digest"]) == 0  # повторный digest промпт агента не затирает
    assert "Node 20" in (path / "image_prompt.md").read_text(encoding="utf-8")
    assert main(["news", "cover", "--date", meta.added]) == 0
    assert "Art style:" in capsys.readouterr().out
    (path / "text.md").write_text("# Дайджест\n\nВышел [Go 1.26](https://go.dev/blog/go1.26).\n")
    assert main(["news", "check", str(path)]) == 0

    assert main(["news", "sources"]) == 0
    assert "1/1" in capsys.readouterr().out  # go_blog: 1 важный из 1 разобранного


def test_digest_none_is_exit_3(lib, capsys):
    assert main(["news", "digest"]) == 3


class FakeTransport:
    def __init__(self):
        self.calls = []

    def __call__(self, method, params, files=None):
        self.calls.append((method, params, sorted(files or {})))
        return {"ok": True, "result": [] if method == "getUpdates" else {"message_id": 1}}


def gopher_image(path: Path, name: str = "ChatGPT Image.png", size=(1536, 1024)) -> Path:
    """Картинка, которую человек сгенерировал в ChatGPT и положил в папку."""
    from PIL import Image

    out = path / name
    Image.new("RGB", size, (40, 120, 200)).save(out)
    return out


def news_channel_file(root: Path, monkeypatch) -> int:
    """Канал новостей из telegram_tems.md, как у человека в корне репозитория."""
    monkeypatch.delenv(telegram.NEWS_CHANNEL_ENV, raising=False)
    monkeypatch.setenv("NARRATOR_LIBRARY", str(root / "library"))
    (root / telegram.CHANNELS_FILE).write_text(
        "VOICE_NEWS=-1002099747713\nAUDIO_ARTICLE=-1003755616451_14\n", encoding="utf-8"
    )
    return -1002099747713


def test_digest_goes_to_news_channel_as_photo_then_audio(tmp_path, monkeypatch):
    channel_id = news_channel_file(tmp_path, monkeypatch)
    monkeypatch.setenv(telegram.CHANNEL_ENV, "off")
    path = tmp_path / "2026" / "09" / "23" / "digest"
    path.mkdir(parents=True)
    entry = Entry(
        title="Дайджест разработчика, 23 сентября",
        added="2026-09-23",
        summary="Вышел Go 1.26.",
        kind="digest",
        engine="gemini",
        versions={"gemini": 95.0},
        duration_sec=95.0,
    )
    entry.save(path / "meta.json")
    (path / "2026-09-23-digest_gemini.mp3").write_bytes(b"ID3" + b"\0" * 64)
    (path / "post.md").write_text("**Главное**\n🐹 [Вышел Go 1.26](https://go.dev/x)\n")

    fake = FakeTransport()
    state = telegram.State()
    with pytest.raises(telegram.TelegramError):  # картинки нет — ничего не уходит
        telegram.broadcast(telegram.Bot("t", fake), state, path, entry, "gemini")
    assert fake.calls == []

    gopher_image(path)
    done = telegram.broadcast(telegram.Bot("t", fake), state, path, entry, "gemini")
    assert [d.ok for d in done] == [True]
    assert [c[0] for c in fake.calls] == ["sendPhoto", "sendAudio"]
    _, photo, files = fake.calls[0]
    assert photo["chat_id"] == channel_id and files == ["photo"]
    assert '<a href="https://go.dev/x">Вышел Go 1.26</a>' in photo["caption"]
    _, params, files = fake.calls[1]
    assert params["chat_id"] == channel_id
    assert params["caption"] == "Новости за 23 сентября"
    assert params["title"] == "23 сентября" and params["duration"] == 95
    assert files == ["audio", "thumbnail"]
    assert not telegram.is_fresh(state, path, "gemini")

    monkeypatch.setenv(telegram.NEWS_CHANNEL_ENV, "@other")
    assert telegram.recipients(state, entry)[0].chat_id == "@other"
    assert telegram.recipients(state) == []  # статьи — подписчикам, их нет


def test_news_channel_topic_gets_message_thread_id(tmp_path, monkeypatch):
    news_channel_file(tmp_path, monkeypatch)
    (tmp_path / telegram.CHANNELS_FILE).write_text("VOICE_NEWS=-1003755616451_3\n")
    entry = Entry(title="Дайджест разработчика, 1 октября", added="2026-10-01", kind="digest")
    to = telegram.recipients(telegram.State(), entry)[0]
    assert to.address() == {"chat_id": -1003755616451, "message_thread_id": 3}


def test_cover_is_the_newest_image_and_thumb_is_square(tmp_path):
    import os

    from PIL import Image

    from narrator.news import cover

    with pytest.raises(cover.CoverError):
        cover.find_cover(tmp_path)
    old = gopher_image(tmp_path, "old.png")
    os.utime(old, (1, 1))
    gopher_image(tmp_path, "gopher.webp")
    found = cover.find_cover(tmp_path)
    assert found.name == "cover.webp" and not (tmp_path / "gopher.webp").exists()
    thumb = cover.thumbnail(found)
    assert Image.open(thumb).size == (cover.THUMB_SIDE, cover.THUMB_SIDE)
    assert cover.find_cover(tmp_path) == found  # миниатюру за картинку не принимаем


def test_old_meta_without_kind_loads(tmp_path):
    (tmp_path / "meta.json").write_text(
        json.dumps({"title": "Старая", "added": "2026-09-18"}), encoding="utf-8"
    )
    assert Entry.load(tmp_path / "meta.json").kind == "article"


def seed(store: Store, key: str, event: str, importance: int, collected: datetime):
    """Оценённый материал прямо в базу, минуя сбор."""
    store.db.execute(
        "INSERT INTO items (id, source, title, url, collected, status, kind, event, title_ru,"
        " why, factors, authority, score, importance) VALUES"
        " (?, 'go_blog', ?, ?, ?, 'assessed', 'release', ?, ?, 'важно', '{}', 1, ?, ?)",
        (
            key,
            key,
            f"https://go.dev/{key}",
            collected.isoformat(timespec="seconds"),
            event,
            key,
            importance / 10,
            importance,
        ),
    )
    store.commit()


def test_weekly_takes_all_important_events_of_the_week(lib, capsys):
    from narrator.news.store import db_path

    now = datetime.now(UTC)
    with Store(db_path(lib)) as store:
        for n in range(7):  # семь важных событий за неделю — все в обзор, без предела
            seed(store, f"w{n}", f"ev-{n}", 7 + n % 4, now - timedelta(days=n))
        seed(store, "minor", "ev-minor", 6, now)  # важных хватает — 5–6 не добираем
        seed(store, "stale", "ev-stale", 10, now - timedelta(days=9))  # старше недели
        seed(store, "dup", "ev-0", 8, now)  # тот же ev-0 из второго источника

    assert main(["news", "digest"]) == 0  # дайджест дня забирает свои события...
    capsys.readouterr()
    today = date.today().isoformat()
    # ...но в обзор недели они всё равно попадают
    assert main(["news", "weekly", "--date", today]) == 0
    out = capsys.readouterr().out
    path = Path(out.strip().splitlines()[-1])
    assert "событий 7, из них важных (от 7) 7" in out
    events = (path / "events.md").read_text(encoding="utf-8")
    assert "# События недели" in events
    assert "ev-minor" not in events and "ev-stale" not in events
    assert "было в дайджестах дня" in events
    meta = Entry.load(path / "meta.json")
    assert meta.kind == "weekly" and meta.title.startswith("Главное в разработке за неделю")

    assert path.parents[2] == lib / "youtube_news" and path.name.startswith("week_")

    assert main(["news", "check", str(path)]) == 1
    (path / "teleprompter.md").write_text(
        "[M1] " + "Вышел сегодня новый Go. " * 325, encoding="utf-8"
    )
    (path / "post.md").write_text("**Главное за неделю**\n🐹 [Go](https://go.dev)\n")
    meta.summary = "Итоги недели."
    meta.save(path / "meta.json")
    capsys.readouterr()
    assert main(["news", "check", str(path)]) == 1
    assert "нет materials.md" in capsys.readouterr().out
    (path / "materials.md").write_text("## M1 · скриншот\nhttps://go.dev\n", encoding="utf-8")
    assert main(["news", "check", str(path)]) == 0
    assert "1300 слов вслух, в кадре ~10 мин" in capsys.readouterr().out


def test_monthly_adds_loud_industry_events(lib, capsys):
    from narrator.news.store import db_path

    now = datetime.now(UTC)
    with Store(db_path(lib)) as store:
        for n in range(5):  # важных хватает — 5–6 в рубрику разработчика не добираются
            seed(store, f"go{n}", f"ev-go{n}", 8, now)
        seed(store, "deal", "ev-deal", 5, now)  # сделка: мало для Go, но о ней говорят все
        seed(store, "quiet", "ev-quiet", 5, now)
        store.db.execute(
            "UPDATE items SET factors = '{\"community_signal\": 0.9}', weekly = 'w1'"
            " WHERE id = 'deal'"
        )
        store.commit()

    assert main(["news", "monthly"]) == 0
    out = capsys.readouterr().out
    path = Path(out.strip().splitlines()[-1])
    assert path == lib / "youtube_news" / f"{date.today():%Y/%m}" / f"month_{date.today():%Y-%m}"
    events = (path / "events.md").read_text(encoding="utf-8")
    assert "# Рубрика: Важное для разработчика" in events
    assert "# Рубрика: О чём говорила индустрия" in events
    assert "ev-deal" in events and "ev-quiet" not in events
    assert "было в обзорах недели: w1" in events
    assert Entry.load(path / "meta.json").kind == "monthly"
    assert main(["news", "monthly", "--month", "2020-01"]) == 3


def test_teleprompter_check_catches_long_phrases_and_links():
    ok = "[M1] GitHub отключает старые ключи.\n\nПора. [M2] Проверьте свой."
    assert digest.check_teleprompter(ok) == []
    bad = "Смотрите https://x.io и " + "очень " * 40 + "длинную фразу."
    problems = digest.check_teleprompter(bad)
    assert any("ссылки" in p for p in problems)
    assert any("длиннее 30 слов" in p for p in problems)


def test_teleprompter_holds_only_spoken_text_and_insert_marks():
    noisy = "## Тема (1:00–2:00)\n[ЭКРАН: график]\nGitHub **отключает** ключи. /пауза/ Пора."
    problems = digest.check_teleprompter(noisy)
    assert any("заголовки" in p for p in problems)
    assert any("[ЭКРАН: график]" in p for p in problems)
    assert any("/пауз/" in p for p in problems)
    assert any("по порядку" in p for p in digest.check_teleprompter("[M1] Раз. [M3] Два."))


def test_materials_describe_every_insert():
    prompter = "[M1] Раз. [M2] Два."
    assert digest.check_materials(prompter, "## M1 · видео\n## M2 · схема\n") == []
    problems = digest.check_materials(prompter, "## M1 · видео\n## M4 · схема\n")
    assert problems == ["нет описания вставок: M2", "описаны, но в суфлёре их нет: M4"]


def test_week_runs_saturday_to_friday():
    from narrator.news.cli import last_full_week_end

    friday = date(2026, 9, 25)
    assert last_full_week_end(date(2026, 9, 26)) == friday  # суббота — снимаем прошедшую
    assert last_full_week_end(date(2026, 9, 27)) == friday  # воскресенье — её же
    assert last_full_week_end(date(2026, 9, 28)) == friday  # понедельник — её же
    assert last_full_week_end(friday) == date(2026, 9, 18)  # в пятницу неделя не кончилась


def test_monthly_title():
    from narrator.news.cli import monthly_title

    assert monthly_title(date(2026, 9, 1)) == "Главное в разработке за сентябрь 2026"


def test_weekly_title_spans_months():
    from narrator.news.cli import weekly_title

    assert weekly_title(date(2026, 9, 23), 7) == "Главное в разработке за неделю, 17–23 сентября"
    assert weekly_title(date(2026, 10, 2), 7).endswith("26 сентября – 2 октября")


def test_digest_script_over_20_minutes_fails_check(lib, capsys):
    path = lib / "2026" / "09" / "23" / "digest"
    path.mkdir(parents=True)
    Entry(title="Д", added="2026-09-23", summary="С.", kind="digest").save(path / "meta.json")
    (path / "post.md").write_text("**Главное**\n🐹 Вышел Го\n")
    long = "Сегодня вышла новая версия языка. " * (digest.DIGEST_MAX_WORDS // 5 + 10)
    (path / "narration.md").write_text(long, encoding="utf-8")
    assert main(["news", "check", str(path)]) == 1
    assert "дольше 20 мин" in capsys.readouterr().out


def test_old_database_gets_weekly_column(tmp_path):
    import sqlite3

    db = tmp_path / "news.db"
    old = sqlite3.connect(db)
    old.execute(
        "CREATE TABLE items (id TEXT PRIMARY KEY, source TEXT, title TEXT, url TEXT,"
        " published TEXT, summary TEXT, collected TEXT, status TEXT, kind TEXT,"
        " event TEXT, title_ru TEXT, why TEXT, factors TEXT, authority REAL, score REAL,"
        " importance INTEGER, confirmed_by TEXT, assessed TEXT, digest TEXT)"
    )
    old.commit()
    old.close()
    with Store(db) as store:
        cols = {r["name"] for r in store.db.execute("PRAGMA table_info(items)")}
    assert {"weekly", "monthly"} <= cols


def test_old_scale_is_rescaled_to_ten(tmp_path):
    with Store(tmp_path / "news.db") as store:
        seed(store, "plain", "ev-a", 5, NOW)  # балл 0.5 → 6 из 10
        seed(store, "capped", "ev-b", 5, NOW)
        store.db.execute("UPDATE items SET score = 0.95, importance = 3 WHERE id = 'capped'")
        store.db.execute("UPDATE items SET importance = 3 WHERE id = 'plain'")
        store.db.execute("DELETE FROM memo")
        store.commit()
    with Store(tmp_path / "news.db") as store:
        levels = {r["id"]: r["importance"] for r in store.db.execute("SELECT * FROM items")}
    assert levels == {"plain": 6, "capped": digest.UNCONFIRMED_CAP}


def test_jargon_counts_what_is_hard_to_hear():
    text = (
        "GitHub убирает ssh-rsa на SHA-1 и добавляет ML-KEM. Нужен x/crypto и ключ Ed25519."
        " Раньше мы писали эс эс аш и эр эс эй. Postgres и Claude — просто названия."
    )
    assert digest.jargon(text) == ["SHA-1", "ML-KEM", "x/crypto", "Ed25519", "эс эс аш", "эр эс эй"]
    assert digest.jargon("Гитхаб выключает старые способы шифрования.") == []


def test_heavy_narration_fails_check(lib, capsys):
    path = lib / "2026" / "09" / "23" / "digest"
    path.mkdir(parents=True)
    Entry(title="Д", added="2026-09-23", summary="С.", kind="digest").save(path / "meta.json")
    (path / "image_prompt.md").write_text(filled_prompt(date(2026, 9, 23)), encoding="utf-8")
    (path / "post.md").write_text("**Главное**\n🐹 Вышел Go\n")
    (path / "text.md").write_text("# Д\n")
    heavy = "GitHub убирает ssh-rsa на SHA-1, добавляет ML-KEM и sntrup761. " * 10
    (path / "narration.md").write_text(heavy, encoding="utf-8")
    assert main(["news", "check", str(path)]) == 1
    out = capsys.readouterr().out
    assert "тяжело слушать" in out and "SHA-1 ×10" in out

    light = "GitHub выключает старые способы шифрования, пора обновить клиенты. " * 10
    (path / "narration.md").write_text(light, encoding="utf-8")
    assert main(["news", "check", str(path)]) == 0
    assert "make news-publish" in capsys.readouterr().out


def test_publish_finds_manual_mp3_and_image(lib, monkeypatch, capsys):
    from narrator import cli as app
    from narrator.news import cli as news_cli

    channel_id = news_channel_file(lib.parent, monkeypatch)
    monkeypatch.setattr(news_cli, "audio_seconds", lambda p: 420.0)
    fake = FakeTransport()
    state = telegram.State()
    monkeypatch.setattr(app, "open_bot", lambda: (telegram.Bot("t", fake), state, lib / "t.json"))

    path = lib / "2026" / "09" / "23" / "digest"
    path.mkdir(parents=True)
    Entry(
        title="Дайджест разработчика, 23 сентября",
        added="2026-09-23",
        summary="С.",
        kind="digest",
    ).save(path / "meta.json")
    (path / "post.md").write_text("**Главное**\n🐹 [Вышел Go](https://go.dev/x)\n")
    (path / "2026-09-23-digest_espeech.mp3").write_bytes(b"old")  # старая озвучка — не она

    assert main(["news", "publish", "--as", "photo", "--date", "2026-09-23"]) == 2
    assert "нет mp3" in capsys.readouterr().err

    (path / "Gemini digest.mp3").write_bytes(b"ID3" + b"\0" * 64)
    assert main(["news", "publish", "--as", "photo", "--date", "2026-09-23"]) == 2
    assert "нет картинки" in capsys.readouterr().err

    image = gopher_image(lib, "from-chatgpt.png")  # лежит вне папки — через --image
    monkeypatch.setenv(news_cli.POST_ENV, "photo")  # вид поста — из окружения
    assert main(["news", "publish", "--date", "2026-09-23", "--image", str(image)]) == 0
    audio = path / "2026-09-23-digest_gemini.mp3"
    assert audio.exists() and not (path / "Gemini digest.mp3").exists()
    assert [c[0] for c in fake.calls if c[0] != "getUpdates"] == ["sendPhoto", "sendAudio"]
    post = next(c[1] for c in fake.calls if c[0] == "sendAudio")
    assert post["chat_id"] == channel_id and post["duration"] == 420
    assert (path / "cover.png").exists() and (path / "cover-thumb.jpg").exists()
    meta = Entry.load(path / "meta.json")
    assert meta.engine == "gemini" and meta.versions == {"gemini": 420.0}

    fake.calls.clear()
    assert main(["news", "publish", str(path)]) == 0  # mp3 тот же — второй раз не шлём
    assert "уже в канале" in capsys.readouterr().out
    assert not [c for c in fake.calls if c[0] != "getUpdates"]

    # в названный канал выпуск уходит отдельно: у него своя отметка
    assert main(["news", "publish", str(path), "--channel", "audio_article"]) == 0
    post = next(c[1] for c in fake.calls if c[0] == "sendPhoto")
    assert post["chat_id"] == -1003755616451 and post["message_thread_id"] == 14
    fake.calls.clear()
    assert main(["news", "publish", str(path), "--channel", "audio_article"]) == 0
    assert "уже в канале" in capsys.readouterr().out
    assert main(["news", "publish", str(path), "--channel", "podcast"]) == 4
    assert "канала podcast нет" in capsys.readouterr().err
    assert not [c for c in fake.calls if c[0] != "getUpdates"]


def test_digest_sections_add_youtube_and_telegram():
    sources = {
        "blog": src(id="blog"),
        "yt": src(id="yt", type="youtube_expert"),
        "tg": src(id="tg", type="telegram_channel"),
    }

    def ev(key, source, level):
        row = {"source": source, "importance": level, "score": level / 10}
        return digest.Event(key, level / 10, level, [row])

    events = [
        ev("release", "blog", 9),
        ev("talk", "yt", 6),
        ev("video", "yt", 5),
        ev("boring", "yt", 3),
        ev("post", "tg", 4),
    ]
    sections = digest.select_sections(events, sources)
    names = [(name, [e.key for e in part]) for name, part in sections]
    assert names == [
        ("Релизы и официальные новости", ["release"]),
        ("YouTube и конференции", ["talk", "video"]),
        ("Телеграм-каналы", ["post"]),
    ]


def vendor_events():
    sources = {
        "gh_changelog": src(id="gh_changelog"),
        "copilot_changelog": src(id="copilot_changelog", vendor="gh"),
        "pg_news": src(id="pg_news"),
        "brooker": src(id="brooker", type="expert_blog"),
    }

    def ev(key, source, level, kind="new_feature"):
        row = {"source": source, "importance": level, "score": level / 10, "kind": kind}
        return digest.Event(key, level / 10, level, [row])

    return sources, ev


def test_main_takes_at_most_two_events_per_vendor():
    sources, ev = vendor_events()
    events = [
        ev("gh1", "gh_changelog", 8),
        ev("gh2", "copilot_changelog", 8),
        ev("gh3", "gh_changelog", 8),
        ev("gh-cve", "gh_changelog", 7, "security"),
        ev("pg", "pg_news", 7),
    ]
    main = digest.select_sections(events, sources)[0][1]
    assert [e.key for e in main] == ["gh1", "gh2", "gh-cve", "pg"]


def test_all_important_events_go_in_and_must_know_ignores_top():
    sources, ev = vendor_events()
    events = [ev(f"pg{n}", "pg_news", 8) for n in range(2)]
    events += [ev(f"gh{n}", "gh_changelog", 9) for n in range(7)]  # 9 — обязательное
    main = digest.select_sections(events, sources, budget=10**6)[0][1]
    assert len(main) == 9  # пятёрки больше нет
    capped = digest.select_sections(events, sources, top=3, budget=10**6)[0][1]
    assert len(capped) == 7  # --top не режет обязательные


def test_long_issue_makes_lesser_themes_brief_and_drops_extras():
    sources, ev = vendor_events()
    sources["yt"] = src(id="yt", type="youtube_expert")
    events = [ev("cve", "pg_news", 7, "security")]
    events += [ev(f"pg{n}", "pg_news", 7) for n in range(2)]
    events += [ev(f"gh{n}", "gh_changelog", 8) for n in range(2)]
    events += [ev("video", "yt", 6)]
    budget = digest.FRAME_WORDS + 2 * digest.FULL_WORDS + 3 * digest.BRIEF_WORDS
    sections = digest.select_sections(events, sources, budget=budget)
    main = sections[0][1]
    assert len(sections) == 1  # YouTube не вытесняет новости
    assert len(main) == 5
    assert sorted(e.key for e in main if not e.brief) == ["cve", "gh0"]
    words = digest.FRAME_WORDS + 2 * digest.FULL_WORDS + 3 * digest.BRIEF_WORDS
    assert digest.issue_length(sections).startswith(f"Оценка длины: ~{words} слов")


def test_quiet_day_has_room_for_youtube_and_deep_dive():
    sources, ev = vendor_events()
    sources["yt"] = src(id="yt", type="youtube_expert")
    events = [ev("pg", "pg_news", 8), ev("essay", "brooker", 6), ev("video", "yt", 5)]
    names = [name for name, _ in digest.select_sections(events, sources)]
    assert names == [
        "Релизы и официальные новости",
        "Инженерные блоги",
        "YouTube и конференции",
    ]


def test_vendor_from_recent_digests_goes_one_level_lower():
    sources, ev = vendor_events()
    events = [
        ev("gh", "gh_changelog", 7),
        ev("gh-cve", "gh_changelog", 7, "security"),
        ev("pg", "pg_news", 7),
        ev("pg2", "pg_news", 6),
    ]
    main = digest.select_sections(events, sources, recent={"gh"})[0][1]
    assert [e.key for e in main] == ["gh-cve", "pg", "gh"]  # gh добран как 6
    assert main[2].importance == 7  # в выпуске — своя важность


def test_best_deep_dive_joins_main_when_none_made_it():
    sources, ev = vendor_events()
    events = [ev(f"pg{n}", "pg_news", 8) for n in range(2)] + [
        ev("essay", "brooker", 6, "engineering_deep_dive"),
        ev("meh", "brooker", 5),
    ]
    sections = digest.select_sections(events, sources)
    assert ("Инженерные блоги", [events[2]]) in sections


def test_minor_product_change_is_capped():
    minor = dict(production_impact=0.3, global_impact=0.5)
    wide = dict(production_impact=0.3, global_impact=0.8)
    assert digest.final_importance(0.8, False, None, "new_feature", minor) == 6
    assert digest.final_importance(0.8, False, None, "new_feature", wide) == 9
    assert digest.final_importance(0.8, False, None, "security", minor) == 9


def test_old_assessments_are_rescored_once(lib, monkeypatch):
    from narrator.news import cli
    from narrator.news.store import db_path

    old = dict(global_impact=0.5, stack_relevance=1, novelty=0.7, production_impact=0.3)
    old["community_signal"] = 0.3
    with Store(db_path(lib)) as store:
        store.db.execute(
            "INSERT INTO items (id, source, title, url, collected, status, kind, event,"
            " factors, score, importance) VALUES ('a', 'go_blog', 't', 'u', '2026-10-01',"
            " 'assessed', 'new_feature', 'e', ?, 0.665, 7)",
            (json.dumps(old),),
        )
    with cli.open_store() as store:
        r = store.get("a")
        assert json.loads(r["factors"])["insight"] == digest.LEGACY_INSIGHT_DEFAULT
        assert r["importance"] == 6  # мелкая фича поставщика больше не «важное»
        assert store.memo()["scoring"] == digest.SCORING_VERSION


def test_go_vulndb_takes_latest_reviewed_entries():
    index = [
        {"id": "GO-2026-0001", "modified": "2026-10-01T00:00:00Z"},
        {"id": "GO-2026-0002", "modified": "2026-10-14T00:00:00Z"},
        {"id": "GO-2026-0003", "modified": "2026-10-13T00:00:00Z"},
    ]
    base = "https://vuln.go.dev"

    def osv(n, status="REVIEWED"):
        return json.dumps(
            {
                "id": f"GO-2026-000{n}",
                "published": "2026-10-14T10:00:00Z",
                "summary": "Request smuggling in net/http",
                "details": "Подробности.",
                "aliases": ["CVE-2026-1", "GHSA-xxxx"],
                "affected": [{"package": {"name": "stdlib"}}],
                "database_specific": {
                    "url": f"https://pkg.go.dev/vuln/GO-2026-000{n}",
                    "review_status": status,
                },
            }
        )

    pages = {f"{base}/index/vulns.json": json.dumps(index)}
    pages |= {f"{base}/ID/GO-2026-000{n}.json": osv(n) for n in (1, 2)}
    pages[f"{base}/ID/GO-2026-0003.json"] = osv(3, "UNREVIEWED")
    items = feeds.fetch(src(kind="govuln"), getter(pages))
    assert [i.url for i in items] == [
        "https://pkg.go.dev/vuln/GO-2026-0002",
        "https://pkg.go.dev/vuln/GO-2026-0001",
    ]
    assert items[0].title == "GO-2026-0002: Request smuggling in net/http"
    assert "stdlib" in items[0].summary and "CVE-2026-1" in items[0].summary
    assert items[0].published == "2026-10-14T10:00:00+00:00"


def test_http_get_retries_cut_off_response(monkeypatch):
    import http.client
    import io

    calls = []

    class Response(io.BytesIO):
        headers = http.client.HTTPMessage()

        def read(self, *a):
            calls.append(1)
            if len(calls) == 1:
                raise http.client.IncompleteRead(b"half")
            return b"whole"

    monkeypatch.setattr(feeds.urllib.request, "urlopen", lambda *a, **k: Response())
    assert feeds.http_get("https://x.test") == "whole"
    calls.clear()
    monkeypatch.setattr(feeds.urllib.request, "urlopen", lambda *a, **k: Response())
    with pytest.raises(FetchError, match="ответ оборвался"):
        feeds.http_get("https://x.test", attempts=1)


def test_http_get_refuses_oversized_response(monkeypatch):
    # SEC-009: лента больше лимита — ошибка источника, а не падение и не сотни МБ в памяти.
    import http.client
    import io

    asked = []

    class Response(io.BytesIO):
        headers = http.client.HTTPMessage()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            asked.append(n)
            return super().read(n)

    big = b"x" * (feeds.MAX_RESPONSE_BYTES + 100)
    monkeypatch.setattr(feeds.urllib.request, "urlopen", lambda *a, **k: Response(big))
    with pytest.raises(FetchError, match="ответ больше"):
        feeds.http_get("https://x.test")
    assert asked == [feeds.MAX_RESPONSE_BYTES + 1]
    monkeypatch.setattr(feeds.urllib.request, "urlopen", lambda *a, **k: Response(b"<rss/>"))
    assert feeds.http_get("https://x.test") == "<rss/>"


def test_ghsa_filters_by_severity_and_shows_epss():
    advisories = [
        {
            "ghsa_id": "GHSA-aaaa",
            "cve_id": "CVE-2026-2",
            "html_url": "https://github.com/advisories/GHSA-aaaa",
            "summary": "Auth bypass in x/crypto",
            "description": "Описание.",
            "severity": "critical",
            "published_at": "2026-10-14T08:00:00Z",
            "vulnerabilities": [{"package": {"ecosystem": "go", "name": "golang.org/x/crypto"}}],
            "epss": {"percentage": 1.5, "percentile": 90},
        },
        {
            "ghsa_id": "GHSA-bbbb",
            "html_url": "https://github.com/advisories/GHSA-bbbb",
            "summary": "minor",
            "severity": "low",
        },
    ]
    url = "https://api.github.com/advisories?ecosystem=go&per_page=30"
    url += "&affects=golang.org/x/crypto,github.com/jackc/pgx/v5"
    packages = ["golang.org/x/crypto", "github.com/jackc/pgx/v5"]
    s = src(kind="ghsa", ecosystem="go", min_severity="high", packages=packages)
    items = feeds.fetch(s, getter({url: json.dumps(advisories)}))
    assert [i.title for i in items] == ["GHSA-aaaa / CVE-2026-2: Auth bypass in x/crypto"]
    assert "EPSS 1.5%" in items[0].summary and "golang.org/x/crypto" in items[0].summary


def test_pilot_channel_ceiling_holds_even_when_confirmed():
    assert digest.final_importance(0.95, True, "https://go.dev", ceiling=6) == 6


def test_source_can_be_marked_secondary_outside_confirm_types(tmp_path):
    cfg = tmp_path / "s.toml"
    cfg.write_text(
        "[types]\nexpert_blog = 0.85\n[confirm]\nsecondary = []\n"
        '[[source]]\nid = "phil"\nname = "P"\nurl = "u"\ntype = "expert_blog"\n'
        'category = "backend"\nkind = "feed"\nfeed = "f"\nsecondary = true\n',
        encoding="utf-8",
    )
    assert sources.load(cfg)[0].secondary


def test_newsletter_alone_is_not_a_digest_topic():
    srcs = {"gw": src(id="golang_weekly", type="newsletter"), "go_blog": src()}

    def ev(key, *names):
        rows = [{"source": n, "importance": 6, "score": 0.6, "kind": "release"} for n in names]
        return digest.Event(key, 0.6, 6, rows)

    expanded = ev("expanded", "gw")
    expanded.items[0]["confirmed_by"] = "https://go.dev/blog/x"
    events = [ev("only-weekly", "gw"), ev("with-primary", "gw", "go_blog"), expanded]
    chosen = [e.key for _, part in digest.select_sections(events, srcs) for e in part]
    assert sorted(chosen) == ["expanded", "with-primary"]


def test_source_without_any_digest_hit_is_flagged(lib, capsys):
    from narrator.news.store import db_path

    with Store(db_path(lib)) as store:
        for n in range(16):
            store.db.execute(
                "INSERT INTO items (id, source, title, url, collected, status)"
                " VALUES (?, 'go_blog', 't', 'u', '2026-10-01', 'skipped')",
                (f"i{n}",),
            )
    assert main(["news", "sources"]) == 0
    assert "мало пользы: разобрано 16" in capsys.readouterr().out


def test_collect_in_sandbox_does_not_overwrite_last_run(lib, monkeypatch):
    from narrator.news.store import db_path

    def blocked(s, get=None, memo=None):
        raise FetchError(f"{s.url}: <urlopen error Tunnel connection failed: 403 Forbidden>")

    monkeypatch.setattr(feeds, "fetch", blocked)
    assert main(["news", "collect"]) == 2
    with Store(db_path(lib)) as store:
        assert store.db.execute("SELECT count(*) FROM runs").fetchone()[0] == 0


def test_youtube_channel_id_from_config_skips_handle_page():
    source = Source(
        "yt",
        "YT",
        "https://y",
        "youtube_expert",
        "youtube_en",
        "youtube",
        handle="Wrong",
        channel_id="UC123",
    )
    feed = "https://www.youtube.com/feeds/videos.xml?channel_id=UC123"
    assert feeds.fetch(source, getter({feed: ATOM}), {})


def test_youtube_falls_back_to_uploads_playlist():
    s = src(kind="youtube", handle="x", channel_id="UCabc")
    base = "https://www.youtube.com/feeds/videos.xml?playlist_id="
    assert len(feeds.fetch(s, getter({base + "UULFabc": ATOM}), {})) == 2
    assert len(feeds.fetch(s, getter({base + "UUabc": ATOM}), {})) == 2
    with pytest.raises(FetchError, match="channel_id=UCabc: HTTP 404"):
        feeds.fetch(s, getter({}), {})


def test_digest_for_a_given_day_takes_only_that_days_news(lib, capsys):
    from narrator.news.cli import day_bounds, parse_day
    from narrator.news.store import db_path

    day = date(2026, 9, 22)
    start, end = day_bounds(day)
    now = datetime.now(UTC)
    with Store(db_path(lib)) as store:
        seed(store, "on-day", "ev-day", 8, now)
        seed(store, "day-before", "ev-before", 9, now)
        seed(store, "undated", "ev-undated", 8, start.astimezone(UTC) + timedelta(hours=3))
        store.db.execute(
            "UPDATE items SET published = ? WHERE id = 'on-day'",
            ((start + timedelta(hours=12)).astimezone(UTC).isoformat(timespec="seconds"),),
        )
        store.db.execute(
            "UPDATE items SET published = ? WHERE id = 'day-before'",
            ((start - timedelta(minutes=1)).astimezone(UTC).isoformat(timespec="seconds"),),
        )
        store.commit()

    assert main(["news", "digest", "--date", "2026-09-22"]) == 0
    path = Path(capsys.readouterr().out.strip().splitlines()[-1])
    assert path == lib / "2026" / "09" / "22" / "digest"
    events = (path / "events.md").read_text(encoding="utf-8")
    assert "ev-day" in events and "ev-undated" in events and "ev-before" not in events
    assert Entry.load(path / "meta.json").title.endswith("22 сентября")

    assert parse_day("yesterday") == date.today() - timedelta(days=1)
    assert parse_day("вчера") == parse_day("yesterday") and parse_day(None) is None
    assert end - start == timedelta(days=1)


needs_ffmpeg = pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="нет ffmpeg")


@needs_ffmpeg
def test_video_is_one_post_with_static_image(lib, monkeypatch, capsys):
    import subprocess

    from narrator import cli as app

    channel_id = news_channel_file(lib.parent, monkeypatch)
    fake = FakeTransport()
    state = telegram.State()
    monkeypatch.setattr(app, "open_bot", lambda: (telegram.Bot("t", fake), state, lib / "t.json"))

    path = lib / "2026" / "09" / "23" / "digest"
    path.mkdir(parents=True)
    Entry(
        title="Дайджест разработчика, 23 сентября",
        added="2026-09-23",
        summary="С.",
        kind="digest",
    ).save(path / "meta.json")
    (path / "post.md").write_text("**Главное**\n🐹 [Вышел Go](https://go.dev/x)\n")
    silence = ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=24000:cl=mono"]
    subprocess.run([*silence, "-t", "2", str(path / "Gemini.mp3")], check=True)
    gopher_image(path, size=(1025, 683))  # нечётные стороны — ffmpeg их выровняет

    assert main(["news", "video", "--date", "2026-09-23", "--no-send"]) == 0
    video = path / "2026-09-23-digest.mp4"
    assert video.exists() and not fake.calls

    assert main(["news", "video", "--date", "2026-09-23"]) == 0
    sent = [c for c in fake.calls if c[0] != "getUpdates"]
    assert [c[0] for c in sent] == ["sendVideo"]  # один пост
    _, params, files = sent[0]
    assert params["chat_id"] == channel_id and files == ["cover", "thumbnail", "video"]
    assert params["cover"] == "attach://cover"
    assert '<a href="https://go.dev/x">Вышел Go</a>' in params["caption"]
    assert (params["width"], params["height"]) == (1024, 682) and params["duration"] == 2

    # первый кадр — обложка с кнопкой «плей» по центру, дальше — картинка дня
    def center(at: str) -> tuple:
        from PIL import Image

        shot = path.parent / f"shot-{at}.png"
        grab = ["ffmpeg", "-loglevel", "error", "-ss", at, "-i", str(video), "-frames:v", "1"]
        subprocess.run([*grab, str(shot)], check=True)
        with Image.open(shot) as img:
            return img.convert("RGB").getpixel((img.width // 2 + 20, img.height // 2))

    assert min(center("0")) > 200  # белый треугольник кнопки
    assert center("1") == pytest.approx((40, 120, 200), abs=12)
    with __import__("PIL.Image").Image.open(path / "video-thumb.jpg") as thumb:
        assert max(thumb.size) <= 320
    from narrator.news.cover import find_cover

    assert find_cover(path).name == "cover.png"  # кадры видео — не картинка дня
    assert not telegram.is_fresh(state, path, "gemini")  # отметка общая с photo

    fake.calls.clear()  # ушёл видео — ни видео, ни картинкой второй раз не уходит
    assert main(["news", "publish", "--date", "2026-09-23"]) == 0
    assert main(["news", "publish", "--as", "photo", "--date", "2026-09-23"]) == 0
    assert "уже в канале" in capsys.readouterr().out
    assert not [c for c in fake.calls if c[0] != "getUpdates"]
    assert main(["news", "publish", "--date", "2026-09-23", "--force"]) == 0
    assert [c[0] for c in fake.calls if c[0] != "getUpdates"] == ["sendVideo"]


def test_post_format_from_flag_env_or_video(monkeypatch):
    from narrator.news import cli as news_cli

    monkeypatch.delenv(news_cli.POST_ENV, raising=False)
    assert news_cli.post_format(None) == "video"
    monkeypatch.setenv(news_cli.POST_ENV, "Photo")
    assert news_cli.post_format(None) == "photo"
    assert news_cli.post_format("video") == "video"
    monkeypatch.setenv(news_cli.POST_ENV, "gif")
    with pytest.raises(SystemExit):
        news_cli.post_format(None)


def test_failed_source_is_rescued_by_agent(lib, tmp_path, monkeypatch, capsys):
    from narrator.news.store import db_path

    def down(s, get=None, memo=None):
        if s.id == "go_blog":
            raise FetchError(f"{s.feed}: HTTP 406")
        return []

    monkeypatch.setattr(feeds, "fetch", down)
    assert main(["news", "collect"]) == 0
    assert "news failed" in capsys.readouterr().out

    assert main(["news", "failed"]) == 0
    out = capsys.readouterr().out
    assert "`go_blog`" in out and "HTTP 406" in out and "hackernews" not in out

    today = date.today().isoformat()
    data = {
        "items": [
            {
                "source": "go_blog",
                "url": "https://go.dev/blog/go1.26",
                "title": "Go 1.26",
                "published": today,
                "summary": "<p>Вышел</p>",
            },
            {"source": "go_blog", "url": "https://go.dev/blog/undated", "title": "Без даты"},
            {"source": "go_blog", "url": "ftp://x", "title": "плохая ссылка"},
            {"source": "nope", "url": "https://x.org/a", "title": "чужой"},
        ],
    }
    file = tmp_path / "rescue.json"
    file.write_text(json.dumps(data), encoding="utf-8")
    assert main(["news", "add", str(file)]) == 1  # ошибки есть, но хорошее сохранено
    captured = capsys.readouterr()
    assert "материалов   2, новых 2" in captured.out
    assert "без добора осталось 0" in captured.out
    assert "url не http(s)" in captured.err and "nope: нет такого источника" in captured.err

    with Store(db_path(lib)) as store:
        row = store.get(item_id("https://go.dev/blog/go1.26"))
        assert row["status"] == "new" and row["summary"] == "Вышел"
        assert row["published"].startswith(today)
        assert store.get(item_id("https://go.dev/blog/undated"))["status"] == "new"
        assert store.failed() == {}

    assert main(["news", "failed"]) == 0
    assert "добирать нечего" in capsys.readouterr().out
    assert main(["news", "sources"]) == 0
    assert "HTTP 406 · добрал агент: 2" in capsys.readouterr().out  # сбой виден и дальше

    # следующий сбор снова упал — источник снова ждёт добора
    assert main(["news", "collect"]) == 0
    capsys.readouterr()
    assert main(["news", "failed", "--json"]) == 0
    assert [r["source"] for r in json.loads(capsys.readouterr().out)] == ["go_blog"]

    # проверил, а свежего нет — тоже добор
    file.write_text(json.dumps({"checked": ["go_blog"]}), encoding="utf-8")
    assert main(["news", "add", str(file)]) == 0
    assert "свежего нет" in capsys.readouterr().out
    assert main(["news", "failed"]) == 0
    assert "добирать нечего" in capsys.readouterr().out


def test_review_verdict_parsing():
    assert digest.review_verdict("# Судья\n\nВердикт: готово\n") == "готово"
    assert digest.review_verdict("**Вердикт:** готово с оговорками.") == "готово с оговорками"
    assert digest.review_verdict("Вердикт: Не готово") == "не готово"
    assert digest.review_verdict("Вердикт: норм") is None
    assert digest.review_verdict("без вердикта") is None


def test_judge_verdict_gates_publish(lib, monkeypatch, capsys):
    from narrator import cli as app
    from narrator.news import cli as news_cli

    news_channel_file(lib.parent, monkeypatch)
    monkeypatch.setattr(news_cli, "audio_seconds", lambda p: 60.0)
    fake = FakeTransport()
    state = telegram.State()
    monkeypatch.setattr(app, "open_bot", lambda: (telegram.Bot("t", fake), state, lib / "t.json"))
    path = lib / "2026" / "09" / "23" / "digest"
    path.mkdir(parents=True)
    Entry(
        title="Дайджест разработчика, 23 сентября", added="2026-09-23", summary="С.", kind="digest"
    ).save(path / "meta.json")
    (path / "post.md").write_text("**Главное**\n🐹 [Вышел Go](https://go.dev/x)\n")
    (path / "x.mp3").write_bytes(b"ID3" + b"\0" * 64)
    gopher_image(path)
    publish = ["news", "publish", "--as", "photo", "--date", "2026-09-23"]

    assert main(["news", "where", "--date", "2026-09-23"]) == 0
    assert "Дайджеста за 2026-09-23 нет" in capsys.readouterr().out  # narration.md ещё нет
    (path / "narration.md").write_text("Выпуск.\n")
    assert main(["news", "where", "--date", "2026-09-23"]) == 0
    assert "Судья:    не смотрел" in capsys.readouterr().out

    (path / "review.md").write_text("# Судья\n\nВердикт: не готово\n\nФакт в посте не сходится.\n")
    assert main(publish) == 1
    assert "судья: не готово" in capsys.readouterr().err
    assert not [c for c in fake.calls if c[0] != "getUpdates"]

    assert main([*publish, "--force"]) == 0
    assert "отправляю по --force" in capsys.readouterr().out

    (path / "review.md").write_text("Судья посмотрел, всё хорошо\n")
    assert main(["news", "check", str(path)]) == 1
    assert "нет строки «Вердикт: …»" in capsys.readouterr().out
