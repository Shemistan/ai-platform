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
    assert caption.endswith(f"* {META_NOTE}") and "Вышел Go 1.26" in caption
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
