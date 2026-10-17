"""Отклик на опубликованное: подписчики канала и просмотры поста.

Bot API не отдаёт просмотры сообщений, поэтому:
- подписчики — getChatMemberCount (бот — админ канала), username — getChat;
- просмотры — со страницы `https://t.me/<username>/<id>?embed=1`: она есть
  только у публичных каналов; у приватных просмотров не будет.
У пакетов для ручной загрузки (YouTube, TikTok…) просмотры вносятся руками.

Снимки копятся в записи канала в published.json: `stats: [{at, age_h, views,
members, manual?}]`: первые 7 суток — все, дальше — по одному в неделю (AN-004). Сравнивать посты можно только в
окнах: просмотры за 24ч/72ч/7д (интерполяция по возрасту поста, закрытое окно
замораживается в записи) и прирост подписчиков за сутки после поста за вычетом
базового тренда канала (AN-001).
"""

from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable

from aiplatform.studio import publish

FULL_H = 168  # первые 7 суток снимки хранятся все, дальше — по одному на неделю (AN-004)
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


def sent_at(record: dict) -> float | None:
    """Когда пост вышел: залит руками (published_at) или отправлен в Telegram (sent)."""
    value = record.get("published_at") or record.get("sent")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def add_snapshot(record: dict, snapshot: dict, now: float | None = None) -> dict:
    """Новый снимок в запись канала; пустой (ничего не узнали) не пишется.
    У снимка — возраст поста age_h; закрывшиеся окна просмотров замораживаются в записи."""
    if snapshot.get("views") is None and snapshot.get("members") is None:
        return record
    at = time.time() if now is None else now
    entry = {"at": at, **snapshot}
    start = sent_at(record)
    if start is not None:
        entry["age_h"] = round((at - start) / HOUR, 3)
    history = [*record.get("stats", []), entry]
    frozen = freeze(record | {"stats": history})  # окна — до разреживания, по полной истории
    return frozen | {"stats": thin(history, start)}


def thin(history: list[dict], start: float | None) -> list[dict]:
    """Разреживание снимков (AN-004): первые 7 суток после публикации — все (по ним окна 24/72ч и
    кривая набора), дальше — последний снимок каждой недели. Возраст — от публикации, без неё —
    от первого снимка. За год ежедневных снимков — не больше недели снимков плюс ~52."""
    if not history:
        return history
    base = start if start is not None else min(s["at"] for s in history if isinstance(s.get("at"), (int, float)))
    early, weekly = [], {}
    for snap in history:
        age = (snap["at"] - base) / HOUR if isinstance(snap.get("at"), (int, float)) else 0.0
        if age < FULL_H:
            early.append(snap)
        else:
            weekly[int(age // FULL_H)] = snap  # в неделе остаётся последний
    return early + [weekly[k] for k in sorted(weekly)]


def latest(record: dict) -> dict:
    """Последние известные просмотры и прирост подписчиков с момента публикации (сырые, без окон)."""
    stats = record.get("stats") or []
    views = next((s["views"] for s in reversed(stats) if s.get("views") is not None), None)
    members = next((s["members"] for s in reversed(stats) if s.get("members") is not None), None)
    start = record.get("members_at_publish")
    growth = members - start if members is not None and start is not None else None
    return {"views": views, "members": members, "growth": growth, "at": stats[-1]["at"] if stats else None}


# --- окна: просмотры за 24ч/72ч/7д и прирост подписчиков за сутки после поста (AN-001) ---
# Сырые «последние просмотры» и «подписчики сейчас − при публикации» копятся бесконечно: старый
# пост всегда «выигрывает», а посты одной недели делят один прирост канала. Сравнимы только окна.

HOUR, DAY = 3600.0, 86400.0
WINDOWS = ((24, "views_24h"), (72, "views_72h"), (168, "views_7d"))
TREND_DAYS = 7  # базовый тренд канала — медиана суточного прироста за неделю до поста


def view_points(record: dict) -> list[tuple[float, float]]:
    """(возраст поста в часах, просмотры) по снимкам; у снимков до age_h возраст — от времени поста."""
    start = sent_at(record)
    if start is None:
        return []
    points = []
    for snap in record.get("stats") or []:
        if snap.get("views") is None:
            continue
        age = snap.get("age_h")
        if age is None and isinstance(snap.get("at"), (int, float)):
            age = (snap["at"] - start) / HOUR
        if age is not None and age >= 0:
            points.append((float(age), float(snap["views"])))
    return sorted(points)


def interpolate(points: list[tuple[float, float]], x: float) -> float | None:
    """Значение в x линейно между соседними точками; вне диапазона точек — None."""
    after = next((p for p in points if p[0] >= x), None)
    before = next((p for p in reversed(points) if p[0] <= x), None)
    if after is None or before is None:
        return None
    if after[0] == before[0]:
        return after[1]
    return before[1] + (after[1] - before[1]) * (x - before[0]) / (after[0] - before[0])


def window_views(points: list[tuple[float, float]], hours: float) -> float | None:
    """Просмотры к возрасту hours: окно закрыто, только если есть снимок не раньше него.
    В момент публикации просмотров 0 — от этой точки интерполяция, если ранних снимков нет."""
    if not points or points[-1][0] < hours:
        return None
    return interpolate([(0.0, 0.0), *points], hours)


def freeze(record: dict) -> dict:
    """Закрывшиеся окна — в запись: дальше их не сдвинут ни новые снимки, ни разреживание истории."""
    points = view_points(record)
    out = record
    for hours, key in WINDOWS:
        if out.get(key) is None:
            value = window_views(points, hours)
            if value is not None:
                out = out | {key: round(value)}
    return out


def windows(record: dict, now: float | None = None) -> dict:
    """Окна просмотров (замороженные или по снимкам) и возраст поста; незакрытое окно — None."""
    frozen = freeze(record)
    start = sent_at(record)
    age = round(((time.time() if now is None else now) - start) / HOUR, 1) if start is not None else None
    return {key: frozen.get(key) for _, key in WINDOWS} | {"age_h": age}


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2


class Facts:
    """Подписчики и посты по каналам из всех выпусков: подписчики — общие для канала,
    поэтому прирост поста считается по снимкам всех постов этого канала."""

    def __init__(self, records: list[dict]) -> None:
        self.members: dict[str, list[tuple[float, float]]] = {}
        self.posts: dict[str, list[float]] = {}
        for record in records:
            channel = record.get("channel")
            start = sent_at(record)
            if not channel:
                continue
            points = self.members.setdefault(channel, [])
            if start is not None:
                self.posts.setdefault(channel, []).append(start)
                if record.get("members_at_publish") is not None:
                    points.append((start, float(record["members_at_publish"])))
            for snap in record.get("stats") or []:
                if snap.get("members") is not None and isinstance(snap.get("at"), (int, float)):
                    points.append((float(snap["at"]), float(snap["members"])))
        for channel in self.members:
            self.members[channel] = sorted(set(self.members[channel]))
        for channel in self.posts:
            self.posts[channel] = sorted(set(self.posts[channel]))

    def trend(self, channel: str, start: float) -> float | None:
        """Медиана прироста подписчиков в сутки за TREND_DAYS до поста по промежуткам без постов:
        промежуток не годится, если пост был в нём или за сутки до его начала."""
        posts = self.posts.get(channel, [])
        points = [p for p in self.members.get(channel, []) if start - TREND_DAYS * DAY <= p[0] <= start]
        rates = []
        for (a_at, a_m), (b_at, b_m) in zip(points, points[1:], strict=False):
            if b_at <= a_at or any(a_at - DAY < p <= b_at for p in posts):
                continue
            rates.append((b_m - a_m) / ((b_at - a_at) / DAY))
        return _median(rates) if rates else None

    def growth(self, record: dict) -> dict:
        """growth_24h_attr: прирост за [пост, min(пост + 24ч, следующий пост канала)] минус базовый
        тренд канала за это время. Нет тренда — без вычитания (growth_trend: false). Окно не закрыто
        (нет снимка подписчиков после его конца) — None."""
        empty = {"growth_24h_attr": None, "growth_24h": None, "growth_trend": None, "growth_window_h": None}
        channel, start = record.get("channel"), sent_at(record)
        if not channel or start is None:
            return empty
        later = [p for p in self.posts.get(channel, []) if p > start]
        end = min(start + DAY, later[0]) if later else start + DAY
        points = self.members.get(channel, [])
        m1 = interpolate(points, end)
        m0 = record.get("members_at_publish")
        m0 = float(m0) if m0 is not None else interpolate(points, start)
        if m0 is None or m1 is None:
            return empty
        hours = (end - start) / HOUR
        trend = self.trend(channel, start)
        delta = m1 - m0
        attr = delta - trend * hours / 24 if trend is not None else delta
        return {
            "growth_24h_attr": round(attr, 1),
            "growth_24h": round(delta, 1),
            "growth_trend": trend is not None,
            "growth_window_h": round(hours, 1),
        }


def reach(record: dict, facts: Facts, now: float | None = None) -> dict:
    """Всё об отклике записи для API: сырые последние значения, окна просмотров и прирост за сутки."""
    return latest(record) | windows(record, now) | facts.growth(record)


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


LIVE = (None, "sent", "uploaded")  # состояния вышедшего поста (steps.SENT/UPLOADED); None — записи до журнала
LENGTHS = ((180, "до 3 мин"), (480, "3–8 мин"), (900, "8–15 мин"), (float("inf"), "длиннее 15 мин"))


def _length(seconds: float | None) -> str | None:
    if not seconds:
        return None
    return next(label for limit, label in LENGTHS if seconds <= limit)


def summary(items: list[dict]) -> list[dict]:
    """«Что работает»: средние просмотры за 24ч и прирост подписчиков за сутки после поста (за вычетом
    тренда канала) по сценарию, длине ролика и формату. Считаются только публикации с закрытым окном
    24ч: свежий пост с недобранными сутками и старый с накопленными просмотрами несравнимы."""
    posts = []
    for item in items:
        seen: set[str] = set()
        for record in item.get("published", []):
            # Канал выпуска — один раз: первая вышедшая публикация, повтор не считается (AN-005).
            channel = record.get("channel")
            if record.get("state") not in LIVE or (channel and channel in seen):
                continue
            seen.add(channel)
            if record.get("views_24h") is None:
                continue
            posts.append(
                {
                    "Сценарий": item.get("scenario_label"),
                    "Длина ролика": _length(item.get("video_s")),
                    "Формат": "вертикальный 9:16" if record.get("format") == publish.VERTICAL else "16:9 и квадрат",
                    "views": record["views_24h"],
                    "growth": record.get("growth_24h_attr"),
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
