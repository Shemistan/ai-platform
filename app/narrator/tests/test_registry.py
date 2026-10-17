"""Пометки реестра запрещённых организаций в подписях, видео и проверке дайджеста (CMP-002)."""

import json
from html.parser import HTMLParser
from pathlib import Path

import pytest

from narrator import registry, telegram
from narrator.cli import main
from narrator.library import Entry
from narrator.news.cli import check_footnotes
from narrator.news.digest import visible_length

META_NOTE = (
    "Компания Meta (продукты Facebook и Instagram) признана экстремистской организацией"
    " и запрещена на территории РФ."
)


@pytest.fixture(autouse=True)
def default_registry(monkeypatch):
    monkeypatch.delenv(registry.REGISTRY_ENV, raising=False)
    monkeypatch.setenv(telegram.CHANNEL_ENV, "off")


def balanced(markup: str) -> bool:
    stack, ok = [], [True]

    class Tags(HTMLParser):
        def handle_starttag(self, tag, attrs):
            stack.append(tag)

        def handle_endtag(self, tag):
            if not stack or stack.pop() != tag:
                ok[0] = False

    Tags().feed(markup)
    return ok[0] and not stack


def test_default_copy_matches_platform():
    """Копия списка по умолчанию — та же, что у платформы: правят обе вместе."""
    platform = Path(__file__).resolve().parents[2] / "platform/aiplatform/updates/registry.json"
    if not platform.exists():
        pytest.skip("платформы рядом нет")
    ours = json.loads(registry.DEFAULT_FILE.read_text(encoding="utf-8"))["entries"]
    assert ours == json.loads(platform.read_text(encoding="utf-8"))["entries"]


@pytest.mark.parametrize(
    "text,found",
    [
        ("META", True),
        ("meta", True),
        ("Метой", True),
        ("Меты", True),
        ("Threads", True),
        ("Llama 4", True),
        ("метод", False),
        ("метрика", False),
        ("метаданные", False),
        ("мета-анализ", False),
        ("threads в Python", False),
    ],
)
def test_mentions_case_declensions_products(text, found):
    """CMP-014: поиск тот же, что у платформы: регистр, склонения со стоп-словами, продукты."""
    assert bool(registry.notes(text, registry.load())) is found


def test_registry_from_env_and_fallback(tmp_path, monkeypatch):
    own = tmp_path / "registry.json"
    entry = {
        "id": "x",
        "name": "X",
        "status": "extremist",
        "aliases": ["Икс*"],
        "disclaimer": "X запрещена",
    }
    own.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    monkeypatch.setenv(registry.REGISTRY_ENV, str(own))
    assert registry.notes("Про Иксов и Meta", registry.load()) == ["X запрещена."]
    own.write_text("{битый", encoding="utf-8")
    assert registry.notes("Meta", registry.load()) == [META_NOTE]


def test_fit_caption_keeps_tail():
    sentence = "Компания Meta показала **очки** и [рассказала](https://e.x/a?b=1&c=2) о планах. "
    post = registry.with_footnotes(sentence * 25, "", registry.load())
    body = registry.fit_caption(post)
    assert visible_length(body) <= registry.CAPTION_LIMIT
    assert body.endswith(f"* {META_NOTE}") and balanced(body)
    with pytest.raises(registry.CaptionError):
        registry.fit_caption("Пост.\n\n" + "\n".join(f"* {META_NOTE} {n}" for n in range(12)))


def article(tmp_path: Path, summary: str) -> tuple[Path, Entry]:
    path = tmp_path / "2026" / "09" / "18" / "glasses"
    path.mkdir(parents=True)
    entry = Entry(
        title="Очки", added="2026-09-18", summary=summary, author="А", url="https://x.test/a"
    )
    entry.engine, entry.versions = "silero", {"silero": 60.0}
    return path, entry


def test_describe_adds_footnote_from_script(tmp_path):
    path, entry = article(tmp_path, "Про очки дополненной реальности. " * 60)
    (path / "script.txt").write_text("Компания Мета показала очки.", encoding="utf-8")
    caption = telegram.describe(path, entry, "silero")
    assert caption.endswith(f"* {META_NOTE}")
    assert "ИИ-контент: озвучено ИИ-голосом." in caption  # CMP-011
    assert visible_length(caption) <= telegram.CAPTION_LIMIT
    plain, _ = article(tmp_path / "b", "Про Go.")
    assert "запрещена" not in telegram.describe(plain, _, "silero")


def test_digest_caption_and_audio_caption(tmp_path):
    path = tmp_path / "digest"
    path.mkdir()
    entry = Entry(title="Дайджест разработчика, 23 сентября", added="2026-09-23", kind="digest")
    (path / "post.md").write_text("**Главное**\n🐹 [Вышел Go 1.26](https://go.dev/x)\n")
    (path / "narration.md").write_text("Instagram запустил новую ленту.", encoding="utf-8")
    caption = telegram.digest_caption(path, entry)
    assert caption.endswith(f"* {META_NOTE}\n\n{telegram.DIGEST_AI}") and "Вышел Go 1.26" in caption
    assert META_NOTE in telegram.audio_caption(path, entry, "Новости за 23 сентября")


def test_folder_post_gets_footnote(tmp_path, monkeypatch):
    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path / "library"))
    (tmp_path / telegram.CHANNELS_FILE).write_text("AUDIO=-1002099747713\n", encoding="utf-8")
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "post.md").write_text("Facebook закрыл API.", encoding="utf-8")
    calls = []

    def transport(method, params, files=None):
        calls.append((method, params))
        return {"ok": True, "result": {"message_id": 1}}

    telegram.send_folder(telegram.Bot("t", transport), telegram.State(), folder, "AUDIO")
    assert calls[0][0] == "sendMessage" and META_NOTE in calls[0][1]["text"]


def test_news_check_requires_footnote(tmp_path, capsys):
    post = "**Главное**\n🐹 Meta показала очки — [анонс](https://x.test)\n"
    assert check_footnotes(post) and "* Компания Meta" in check_footnotes(post)[0]
    assert check_footnotes(registry.with_footnotes(post, "", registry.load())) == []
    path = tmp_path / "digest"
    path.mkdir()
    Entry(title="Д", added="2026-09-23", summary="С.", kind="digest").save(path / "meta.json")
    (path / "post.md").write_text(post, encoding="utf-8")
    assert main(["news", "check", str(path)]) == 1
    assert "упоминание без пометки реестра" in capsys.readouterr().out


def test_video_frame_has_note_band(tmp_path):
    from PIL import Image

    from narrator.news import video

    cover = tmp_path / "cover.png"
    Image.new("RGB", (400, 400), (250, 250, 250)).save(cover)
    _, _, frame = video.make_frames(cover, [META_NOTE])
    assert (tmp_path / video.NOTE).read_text(encoding="utf-8") == META_NOTE
    dark = Image.open(frame).convert("RGB").getpixel((2, 398))
    assert max(dark) < 200, "внизу кадра тёмная полоса пометки"
    _, _, frame = video.make_frames(cover)  # пометки ушли — кадр пересобран без полосы
    assert not (tmp_path / video.NOTE).exists()
    assert min(Image.open(frame).convert("RGB").getpixel((2, 398))) > 200


AGENT = {
    "id": "petrov",
    "name": "Иван Петров",
    "status": "foreign_agent",
    "kind": "person",
    "aliases": ["Иван* Петров*"],
    "channels": ["https://t.me/petrov_it", "@petrov_tube"],
}


def test_foreign_agent_parse_and_source_match():
    """CMP-012: иноагент — пометка и маркировка по шаблону, канал узнаётся по адресу."""
    (agent,) = registry.parse([AGENT])
    assert agent.disclaimer == "Иван Петров признан(а) Минюстом РФ иностранным агентом"
    assert "РАСПРОСТРАНЕНО ИНОСТРАННЫМ АГЕНТОМ Иван Петров" in agent.material
    assert registry.notes("Иван Петров рассказал", [agent]) == [f"{agent.disclaimer}."]
    found = registry.source_agent
    assert found(["https://t.me/s/petrov_it"], "Канал", [agent]) is agent
    assert found(["petrov_it"], "Канал", [agent]) is agent
    assert found(["https://www.youtube.com/@petrov_tube"], "Канал", [agent]) is agent
    assert found(["https://t.me/other"], "Иван Петров о Go", [agent]) is agent
    assert found(["https://t.me/petrov_it_fan"], "Другой", [agent]) is None
    post = registry.with_materials("Пост.", [agent.material])
    assert post.startswith(agent.material)
    assert registry.with_materials(post, [agent.material]) == post


def test_foreign_agent_material_marked_in_db_and_caption(tmp_path, monkeypatch):
    """CMP-012: материал из канала иноагента помечен в news.db и маркируется в подписи."""
    from narrator.news import cli as news_cli
    from narrator.news.feeds import Item
    from narrator.news.sources import Source
    from narrator.news.store import Store, db_path, now_utc

    own = tmp_path / "registry.json"
    own.write_text(json.dumps({"entries": [AGENT]}), encoding="utf-8")
    monkeypatch.setenv(registry.REGISTRY_ENV, str(own))
    root = tmp_path / "library"
    monkeypatch.setenv("NARRATOR_LIBRARY", str(root))
    entries = registry.load()
    tube = Source(
        id="petrov",
        name="Petrov IT",
        url="https://t.me/s/petrov_it",
        type="telegram_channel",
        category="telegram",
        kind="telegram",
        channel="petrov_it",
    )
    plain = Source(
        id="go_blog", name="The Go Blog", url="https://go.dev/blog/", type="official",
        category="stack", kind="feed",
    )  # fmt: skip
    assert news_cli.agent_of(tube, entries) == "petrov"
    assert news_cli.agent_of(plain, entries) is None
    path = root / "2026" / "10" / "17" / "digest"
    path.mkdir(parents=True)
    now = now_utc()
    with Store(db_path(root)) as store:
        store.add(tube, [Item("https://t.me/petrov_it/1", "Пост")], now, 3, False, "petrov")
        store.add(plain, [Item("https://go.dev/blog/x", "Go")], now, 3, False, None)
        rows = {r["source"]: r["foreign_agent"] for r in store.db.execute("SELECT * FROM items")}
        assert rows == {"petrov": "petrov", "go_blog": None}
        keys = [r["id"] for r in store.db.execute("SELECT id FROM items")]
        store.mark_issue(keys, "2026-10-17-digest")
    entry = Entry(title="Дайджест разработчика, 17 октября", added="2026-10-17", kind="digest")
    (path / "post.md").write_text("**Дайджест**\n\n🐹 Вышел Go — [релиз](https://go.dev/x)\n")
    caption = telegram.digest_caption(path, entry)
    assert caption.startswith("ДАННОЕ СООБЩЕНИЕ (МАТЕРИАЛ)") and "Вышел Go" in caption
    assert "ИНОСТРАННОГО АГЕНТА Иван Петров" in telegram.audio_caption(path, entry, "Новости")
    other = root / "2026" / "10" / "16" / "digest"
    other.mkdir(parents=True)
    (other / "post.md").write_text("**Дайджест**\n\nПро Go.\n")
    assert "ДАННОЕ" not in telegram.digest_caption(other, entry)


def test_ai_labels_in_caption_mp3_and_video(tmp_path, monkeypatch):
    """CMP-011: строка об ИИ в хвосте подписи (лимит 1024), метка в mp3 и mp4, плашка «ИИ»."""
    from PIL import Image

    from narrator import synth
    from narrator.news import video

    post = registry.with_footnotes("Компания Meta показала очки. " * 60, "", registry.load())
    post = registry.with_ai_line(post, telegram.DIGEST_AI)
    body = registry.fit_caption(post)
    assert visible_length(body) <= registry.CAPTION_LIMIT and body.endswith(telegram.DIGEST_AI)
    calls = []
    monkeypatch.setattr(synth.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(Path, "replace", lambda self, target: target)
    synth.encode_mp3(tmp_path / "a.wav", tmp_path / "a.mp3", "Статья")
    synth.retime_mp3(tmp_path / "a.mp3", tmp_path / "b.mp3", 1.25)
    assert all("AI_GENERATED=1" in cmd for cmd in calls)
    monkeypatch.undo()

    cover = tmp_path / "cover.png"
    Image.new("RGB", (400, 400), (250, 250, 250)).save(cover)
    poster, _, frame = video.make_frames(cover)
    for path in (poster, frame):
        assert max(Image.open(path).convert("RGB").getpixel((14, 14))) < 120, "в углу плашка «ИИ»"
    monkeypatch.setenv(video.BADGE_ENV, "0")
    cover.touch()
    (tmp_path / video.FRAME).unlink()
    _, _, frame = video.make_frames(cover)
    assert min(Image.open(frame).convert("RGB").getpixel((14, 14))) > 200
    seen = []
    monkeypatch.setattr(video.subprocess, "run", lambda cmd, **kw: seen.append(cmd))
    audio = tmp_path / "a.mp3"
    audio.write_bytes(b"ID3")
    video.make_video(poster, frame, audio, tmp_path / "v.mp4", 3.0)
    assert "comment=AI-generated" in seen[0]


def test_send_writes_audit_line(tmp_path, monkeypatch):
    """CMP-015: отправка пишет в audit.jsonl sha256 подписи, сноски и версию реестра."""
    import os

    from narrator import audit

    monkeypatch.setenv("NARRATOR_LIBRARY", str(tmp_path / "library"))
    (tmp_path / telegram.CHANNELS_FILE).write_text("AUDIO=-1002099747713\n", encoding="utf-8")
    folder = tmp_path / "f"
    folder.mkdir()
    (folder / "post.md").write_text("Facebook закрыл API.", encoding="utf-8")

    def transport(method, params, files=None):
        return {"ok": True, "result": {"message_id": 7}}

    bot = telegram.Bot("t", transport)
    telegram.send_folder(bot, telegram.State(), folder, "AUDIO")
    (record,) = audit.read(tmp_path)
    assert (record["action"], record["actor"]) == ("publish", "local")
    assert len(record["caption_sha256"]) == 64
    assert record["footnotes"] == [META_NOTE]
    assert record["registry"] == registry.checked() != ""
    assert record["recipients"] == [-1002099747713]
    log = tmp_path / "audit.jsonl"
    first = log.read_text(encoding="utf-8")
    assert oct(os.stat(log).st_mode & 0o777) == "0o600"
    telegram.mark_sent(telegram.State(), folder, "folder")
    assert log.read_text(encoding="utf-8").startswith(first), "строки только дописываются"
    assert audit.read(tmp_path)[-1]["action"] == "mark_sent"
