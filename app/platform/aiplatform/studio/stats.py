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


MAX_PAGE_BYTES = 2 * 1024 * 1024  # embed-страница поста — десятки КБ; больше не читаем (SEC-009)


def _open(url: str) -> str:
    """Страница, но не больше MAX_PAGE_BYTES: больше — "" (отклик неизвестен), без повторов."""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ai-platform)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read(MAX_PAGE_BYTES + 1)
    return "" if len(body) > MAX_PAGE_BYTES else body.decode("utf-8", "replace")


def _retry_after(exc: urllib.error.HTTPError) -> float | None:
    """Пауза из заголовка Retry-After (секунды); нет или слишком долго — None."""
    try:
        wait = float((exc.headers or {}).get("Retry-After") or 0)
    except (TypeError, ValueError):
        return 0.0
    return wait if wait <= publish.MAX_RETRY_AFTER_S else None


def http_get(url: str, opener: HttpGet | None = None) -> str:
    """Страница целиком или "" при сбое. Чтение — повторы как у publish.call_api (BE-013):
    сеть и 5xx — паузы 1/2/4 с, 429 — ждём Retry-After; прочие 4xx не повторяем."""
    fetch = opener or _open
    for attempt in range(1, publish.ATTEMPTS + 1):
        last = attempt == publish.ATTEMPTS
        try:
            return fetch(url)
        except urllib.error.HTTPError as exc:
            if last:
                return ""
            if exc.code == 429:
                wait = _retry_after(exc)
                if wait is None:
                    return ""
                publish._sleep(wait or publish.backoff(attempt))
            elif exc.code >= 500:
                publish._sleep(publish.backoff(attempt))
            else:
                return ""
        except (urllib.error.URLError, OSError):
            if last:
                return ""
            publish._sleep(publish.backoff(attempt))
    return ""


def sent_at(record: dict) -> float | None:
    """Когда пост вышел: залит руками (published_at) или отправлен в Telegram (sent).
    У пакета, ещё не залитого руками, sent — время сборки пакета, а не выхода поста (AN-003, AN-006)."""
    value = record.get("published_at") or (record.get("sent") if record.get("state") != "packaged" else None)
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
    return with_history(record, [*record.get("stats", []), entry])


def with_history(record: dict, history: list[dict]) -> dict:
    """Запись с новой историей снимков: окна замораживаются по полной истории, потом — разреживание."""
    frozen = freeze(record | {"stats": history})
    return frozen | {"stats": thin(history, sent_at(record))}


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


def _format(record: dict) -> str | None:
    if record.get("post_format") == "photo":
        return "картинка и аудио"
    if not record.get("format"):
        return None
    return "вертикальный 9:16" if record["format"] == publish.VERTICAL else "16:9 и квадрат"


FEW = 5  # меньше постов в строке — «мало данных»: медиана по 2–4 точкам ненадёжна (AN-007)
INDEX_DAYS = 30  # индекс просмотров — к медиане views_24h канала за 30 дней до поста


def quartiles(values: list[float]) -> tuple[float, float, float]:
    """p25, медиана, p75 (линейная интерполяция между точками); одна точка — она же везде."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0], ordered[0], ordered[0]

    def at(q: float) -> float:
        pos = q * (len(ordered) - 1)
        low = int(pos)
        high = min(low + 1, len(ordered) - 1)
        return ordered[low] + (ordered[high] - ordered[low]) * (pos - low)

    return at(0.25), at(0.5), at(0.75)


def _live_posts(items: list[dict]) -> list[tuple[dict, dict]]:
    """(выпуск, запись) вышедших публикаций: канал выпуска — один раз, первая вышедшая (AN-005)."""
    out = []
    for item in items:
        seen: set[str] = set()
        for record in item.get("published", []):
            channel = record.get("channel")
            if record.get("state") not in LIVE or (channel and channel in seen):
                continue
            seen.add(channel)
            out.append((item, record))
    return out


def _indexes(posts: list[tuple[dict, dict]]) -> dict[int, float]:
    """Индекс просмотров поста: views_24h / медиана views_24h его канала за INDEX_DAYS до поста
    (сам пост входит). Сравнивает посты маленького и большого канала. Ключ — id(записи)."""
    by_channel: dict[str, list[tuple[float, float]]] = {}
    for _, record in posts:
        start, views = sent_at(record), record.get("views_24h")
        if record.get("channel") and start is not None and views is not None:
            by_channel.setdefault(record["channel"], []).append((float(start), float(views)))
    out = {}
    for _, record in posts:
        start, views = sent_at(record), record.get("views_24h")
        if views is None or start is None:
            continue
        base = [v for at, v in by_channel.get(record.get("channel"), []) if start - INDEX_DAYS * DAY <= at <= start]
        median = _median(base) if base else 0
        if median > 0:
            out[id(record)] = views / median
    return out


def coverage(items: list[dict]) -> dict:
    """Покрытие «Что работает»: сколько вышедших публикаций учтено и почему остальные выпали —
    окно 24ч ещё не закрыто или просмотров нет (приватный канал, ручной пакет без внесённых цифр)."""
    posts = _live_posts(items)
    counted = sum(1 for _, r in posts if r.get("views_24h") is not None)
    fresh = sum(
        1 for _, r in posts if r.get("views_24h") is None and r.get("age_h") is not None and r["age_h"] < WINDOWS[0][0]
    )
    return {"counted": counted, "total": len(posts), "fresh": fresh, "no_data": len(posts) - counted - fresh}


def _row(label: str, group: list[dict]) -> dict:
    """Строка сводки: n, медиана и p25–p75 просмотров за 24ч, медиана прироста и индекса к каналу."""
    p25, median, p75 = quartiles([p["views"] for p in group])
    growth = [p["growth"] for p in group if p["growth"] is not None]
    index = [p["index"] for p in group if p["index"] is not None]
    return {
        "label": label,
        "posts": len(group),
        "median_views": round(median),
        "p25_views": round(p25),
        "p75_views": round(p75),
        "median_growth": round(_median(growth), 1) if growth else None,
        "median_index": round(_median(index), 2) if index else None,
        "few": len(group) < FEW,
    }


def summary(items: list[dict]) -> list[dict]:
    """«Что работает»: медиана и p25–p75 просмотров за 24ч, медиана прироста подписчиков за сутки после
    поста (за вычетом тренда канала) и индекса к медиане канала — по сценарию, длине ролика и формату.
    Медиана, а не среднее: один вирусный пост не перекашивает строку (AN-007). Считаются только
    публикации с закрытым окном 24ч: свежий пост с недобранными сутками и старый с накопленными
    просмотрами несравнимы; сколько выпало — в coverage()."""
    live = _live_posts(items)
    indexes = _indexes(live)
    posts = [
        {
            "Сценарий": item.get("scenario_label"),
            # Срезы — по полям, записанным в момент публикации (AN-006): длина отправленного
            # файла (шортса у вертикального канала), формат канала тогда, вид поста.
            "Длина ролика": _length(record.get("media_s")),
            "Формат": _format(record),
            "views": record["views_24h"],
            "growth": record.get("growth_24h_attr"),
            "index": indexes.get(id(record)),
        }
        for item, record in live
        if record.get("views_24h") is not None
    ]
    out = []
    for dimension in ("Сценарий", "Длина ролика", "Формат"):
        groups: dict[str, list[dict]] = {}
        for post in posts:
            if post[dimension]:
                groups.setdefault(post[dimension], []).append(post)
        rows = [_row(label, group) for label, group in groups.items()]
        rows.sort(key=lambda r: r["median_views"], reverse=True)
        if rows:
            out.append({"dimension": dimension, "rows": rows})
    return out
