"""Отклик на опубликованное: подписчики канала и просмотры поста.

Bot API не отдаёт просмотры сообщений, поэтому:
- подписчики — getChatMemberCount (бот — админ канала), username — getChat;
- просмотры — со страницы `https://t.me/<username>/<id>?embed=1`: она есть
  только у публичных каналов; у приватных просмотров не будет.
У пакетов для ручной загрузки (YouTube, TikTok…) просмотры вносятся руками.

Снимки копятся в записи канала в published.json: `stats: [{at, views,
members, manual?}]`, не больше KEEP последних.
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from aiplatform.studio import publish

KEEP = 60
EMBED = "https://t.me/{username}/{message_id}?embed=1"
_VIEWS = re.compile(r'class="tgme_widget_message_views"[^>]*>([^<]+)<')
_COUNT = re.compile(r"^\s*([\d.,]+)\s*([KMkм]?)\s*$")
HttpGet = Callable[[str], str]


def parse_count(text: str) -> int | None:
    """«1.2K» → 1200, «15» → 15, «2M» → 2000000."""
    m = _COUNT.match(html.unescape(text))
    if not m:
        return None
    value = float(m[1].replace(",", "."))
    return round(value * {"K": 1000, "k": 1000, "M": 1_000_000, "м": 1_000_000}.get(m[2], 1))


def views_from_embed(page: str) -> int | None:
    m = _VIEWS.search(page)
    return parse_count(m[1]) if m else None


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ai-platform)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return ""


def add_snapshot(record: dict, snapshot: dict) -> dict:
    """Новый снимок в запись канала; пустой (ничего не узнали) не пишется."""
    if snapshot.get("views") is None and snapshot.get("members") is None:
        return record
    history = [*record.get("stats", []), {"at": time.time(), **snapshot}]
    return record | {"stats": history[-KEEP:]}


def latest(record: dict) -> dict:
    """Последние известные просмотры и прирост подписчиков с момента публикации."""
    stats = record.get("stats") or []
    views = next((s["views"] for s in reversed(stats) if s.get("views") is not None), None)
    members = next((s["members"] for s in reversed(stats) if s.get("members") is not None), None)
    start = record.get("members_at_publish")
    growth = members - start if members is not None and start is not None else None
    return {"views": views, "members": members, "growth": growth, "at": stats[-1]["at"] if stats else None}


class Refresher:
    """Обновить отклик у записей published.json. getChat — один раз на канал за проход."""

    def __init__(
        self,
        channels: dict[str, publish.Channel],
        http_post: publish.HttpPost | None = None,
        get: HttpGet | None = None,
    ) -> None:
        self.channels = channels
        self.http_post = http_post
        self.get = get or http_get
        self._chats: dict[str, dict] = {}

    def chat(self, channel: publish.Channel) -> dict:
        if channel.id not in self._chats:
            info = publish.chat_call(channel, "getChat", self.http_post)
            self._chats[channel.id] = {
                "username": info.get("username") if isinstance(info, dict) else None,
                "members": publish.members(channel, self.http_post),
            }
        return self._chats[channel.id]

    def record(self, record: dict) -> dict:
        channel = self.channels.get(record.get("channel", ""))
        if channel is None or channel.type != publish.TELEGRAM or not record.get("message_id") or channel.problem():
            return record
        chat = self.chat(channel)
        views = None
        if chat["username"]:
            page = self.get(EMBED.format(username=chat["username"], message_id=record["message_id"]))
            views = views_from_embed(page)
        return add_snapshot(record | {"public": bool(chat["username"])}, {"views": views, "members": chat["members"]})


LENGTHS = ((180, "до 3 мин"), (480, "3–8 мин"), (900, "8–15 мин"), (float("inf"), "длиннее 15 мин"))


def _length(seconds: float | None) -> str | None:
    if not seconds:
        return None
    return next(label for limit, label in LENGTHS if seconds <= limit)


def summary(items: list[dict]) -> list[dict]:
    """«Что работает»: средние просмотры и прирост подписчиков по сценарию, длине ролика и формату.
    Считаются только публикации, у которых известны просмотры."""
    posts = []
    for item in items:
        for record in item.get("published", []):
            if record.get("views") is None:
                continue
            posts.append(
                {
                    "Сценарий": item.get("scenario_label"),
                    "Длина ролика": _length(item.get("video_s")),
                    "Формат": "вертикальный 9:16" if record.get("format") == publish.VERTICAL else "16:9 и квадрат",
                    "views": record["views"],
                    "growth": record.get("growth"),
                }
            )
    out = []
    for dimension in ("Сценарий", "Длина ролика", "Формат"):
        groups: dict[str, list[dict]] = {}
        for post in posts:
            if post[dimension]:
                groups.setdefault(post[dimension], []).append(post)
        rows = []
        for label, group in groups.items():
            growth = [p["growth"] for p in group if p["growth"] is not None]
            rows.append(
                {
                    "label": label,
                    "posts": len(group),
                    "avg_views": round(sum(p["views"] for p in group) / len(group)),
                    "avg_growth": round(sum(growth) / len(growth), 1) if growth else None,
                }
            )
        rows.sort(key=lambda r: r["avg_views"], reverse=True)
        if rows:
            out.append({"dimension": dimension, "rows": rows})
    return out
