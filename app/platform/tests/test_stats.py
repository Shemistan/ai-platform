"""Отклик каналов: подписчики, просмотры, ручные просмотры и сводка «что работает»."""

import json
import os
import shutil
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

from aiplatform.studio import publish, stats, steps
from tests.fake_telegram import FakeTelegram
from tests.fixtures import ARTICLE, TOKEN
from tests.helpers import StudioCase

EMBED = '<div class="tgme_widget_message_info"><span class="tgme_widget_message_views">1.2K</span></div>'


def fake_post(url, body, ctype):
    if url.endswith("/getChat"):
        return {"ok": True, "result": {"id": -100123, "username": "mychan"}}
    if url.endswith("/getChatMemberCount"):
        return {"ok": True, "result": 150}
    return {"ok": False, "description": "unexpected"}


class StatsUnitTest(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(
            [stats.parse_count(x) for x in ("15", "1.2K", "2M", "3,5K", "много")], [15, 1200, 2000000, 3500, None]
        )
        self.assertEqual(stats.views_from_embed(EMBED), 1200)
        self.assertIsNone(stats.views_from_embed("<html>нет</html>"))

    def test_refresher_and_growth(self):
        channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="-100123", token_env="AIP_TG_TEST")
        pages = []

        def get(url):
            pages.append(url)
            return EMBED

        record = {"channel": "tg", "ok": True, "message_id": 42, "members_at_publish": 120}
        with mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}):
            fresh = stats.Refresher({"tg": channel}, http_post=fake_post, get=get).record(record)
        self.assertEqual(pages, ["https://t.me/mychan/42?embed=1"])
        self.assertTrue(fresh["public"])
        self.assertEqual(
            stats.latest(fresh) | {"at": None},
            {"views": 1200, "members": 150, "growth": 30, "at": None, "manual": None},
        )
        # Без токена или id сообщения — ничего не трогаем.
        self.assertEqual(stats.Refresher({"tg": channel}).record(record), record)

    def test_summary_groups(self):
        def post(views, growth=None, fmt="landscape", media_s=None, **extra):
            return {"views_24h": views, "growth_24h_attr": growth, "format": fmt, "media_s": media_s, **extra}

        # Длина — из записи публикации (media_s отправленного файла), не из основного видео выпуска (AN-006).
        items = [
            {"scenario_label": "Дайджест", "video_s": 999, "published": [post(100, 2, media_s=120)]},
            {"scenario_label": "Дайджест", "video_s": 999, "published": [post(300, media_s=600)]},
            {"scenario_label": "Статья", "video_s": 2000, "published": [post(50, 0, "vertical", 2000), post(None)]},
            {"scenario_label": "Пост", "published": [post(10, fmt="landscape", post_format="photo")]},
        ]
        found = {d["dimension"]: d["rows"] for d in stats.summary(items)}
        row = found["Сценарий"][0]
        self.assertEqual(
            (row["label"], row["posts"], row["median_views"], row["median_growth"]), ("Дайджест", 2, 200, 2.0)
        )
        self.assertEqual((row["p25_views"], row["p75_views"], row["few"]), (150, 250, True))
        self.assertEqual([r["label"] for r in found["Длина ролика"]], ["8–15 мин", "до 3 мин", "длиннее 15 мин"])
        self.assertEqual(
            [r["label"] for r in found["Формат"]], ["16:9 и квадрат", "вертикальный 9:16", "картинка и аудио"]
        )


H, D = 3600.0, 86400.0
T0 = 1_800_000_000.0  # время поста в фикстурах окон


def snap(at, views=None, members=None, **extra):
    return {"at": at, "views": views, "members": members, **extra}


class HttpGetRetryTest(unittest.TestCase):
    """http_get повторяет чтение на 5xx, 429 и сбой сети, как publish.call_api (BE-013)."""

    def setUp(self):
        self.sleeps: list[float] = []
        patch = mock.patch.object(publish, "_sleep", self.sleeps.append)
        patch.start()
        self.addCleanup(patch.stop)

    @staticmethod
    def opener(*queue):
        calls = []

        def fetch(url):
            calls.append(url)
            reply = queue[len(calls) - 1] if len(calls) <= len(queue) else EMBED
            if isinstance(reply, Exception):
                raise reply
            return reply

        return fetch, calls

    @staticmethod
    def http_error(code, headers=None):
        return urllib.error.HTTPError("https://t.me/x", code, "err", headers or {}, None)

    def test_5xx_then_200_snapshot_written(self):
        fetch, calls = self.opener(self.http_error(502))
        channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="-100123", token_env="AIP_TG_TEST")
        record = {"channel": "tg", "ok": True, "message_id": 42}
        with mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}):
            fresh = stats.Refresher(
                {"tg": channel}, http_post=fake_post, get=lambda url: stats.http_get(url, fetch)
            ).record(record)
        self.assertEqual(len(calls), 2)
        self.assertEqual(stats.latest(fresh)["views"], 1200)
        self.assertEqual(len(self.sleeps), 1)

    def test_429_waits_retry_after(self):
        fetch, calls = self.opener(self.http_error(429, {"Retry-After": "7"}))
        self.assertEqual(stats.http_get("u", fetch), EMBED)
        self.assertEqual(self.sleeps, [7.0])

    def test_network_errors_give_up_after_attempts(self):
        fetch, calls = self.opener(*[urllib.error.URLError("down")] * 5)
        self.assertEqual(stats.http_get("u", fetch), "")
        self.assertEqual(len(calls), publish.ATTEMPTS)

    def test_404_and_long_retry_after_not_retried(self):
        fetch, calls = self.opener(self.http_error(404))
        self.assertEqual(stats.http_get("u", fetch), "")
        fetch2, calls2 = self.opener(self.http_error(429, {"Retry-After": "9999"}))
        self.assertEqual(stats.http_get("u", fetch2), "")
        self.assertEqual((len(calls), len(calls2), self.sleeps), (1, 1, []))


class WindowsTest(unittest.TestCase):
    """Окна AN-001: просмотры за 24ч/72ч/7д и прирост подписчиков за сутки после поста."""

    def test_interpolation_between_snapshots(self):
        record = {"sent": T0, "stats": [snap(T0 + 12 * H, 100), snap(T0 + 36 * H, 300)]}
        self.assertEqual(stats.windows(record, now=T0 + 40 * H), {
            "views_24h": 200, "views_72h": None, "views_7d": None, "age_h": 40.0,
        })  # fmt: skip
        # Ранних снимков нет — от нуля в момент публикации.
        self.assertEqual(stats.windows({"sent": T0, "stats": [snap(T0 + 48 * H, 480)]})["views_24h"], 240)
        # Без времени поста окон нет.
        self.assertIsNone(stats.windows({"stats": [snap(T0, 5)]})["views_24h"])

    def test_snapshot_age_and_freeze(self):
        record = stats.add_snapshot({"sent": T0}, {"views": 250, "members": None}, now=T0 + 25 * H)
        self.assertEqual(record["stats"][-1]["age_h"], 25.0)
        self.assertEqual(record["views_24h"], 240)
        self.assertNotIn("views_72h", record)
        # Замороженное окно не сдвигается: ни новыми снимками, ни потерей старых (разреживание).
        record = stats.add_snapshot(record | {"stats": []}, {"views": 5000, "members": None}, now=T0 + 200 * H)
        self.assertEqual(record["views_24h"], 240)
        self.assertEqual(record["views_7d"], round(5000 * 168 / 200))


class ThinTest(unittest.TestCase):
    """Разреживание снимков AN-004: ранние — все, после 7 суток — по одному на неделю."""

    def grow(self, record, times):
        for n, at in enumerate(times):
            record = stats.add_snapshot(record, {"views": 10 * (n + 1), "members": 100 + n}, now=at)
        return record

    def test_early_snapshots_kept(self):
        # 100 снимков за первые ~4 суток (раз в час) и ещё 30 суток раз в сутки.
        times = [T0 + (n + 1) * H for n in range(100)] + [T0 + 5 * D + k * D for k in range(30)]
        record = self.grow({"sent": T0}, times)
        early = [s for s in record["stats"] if s["age_h"] < 168]
        self.assertEqual([s["at"] for s in early], [t for t in times if t - T0 < 168 * H], "ранние все на месте")
        late = [s for s in record["stats"] if s["age_h"] >= 168]
        weeks = [int(s["age_h"] // 168) for s in late]
        self.assertEqual(len(weeks), len(set(weeks)), "после 7 суток — не больше одного снимка в неделю")
        self.assertEqual(record["stats"][-1]["at"], times[-1], "последний снимок остаётся")
        self.assertEqual((record["views_24h"], record["views_72h"]), (240, 720), "окна заморожены до разреживания")

    def test_year_of_daily_snapshots_is_bounded(self):
        record = self.grow({"sent": T0}, [T0 + k * D for k in range(1, 366)])
        self.assertLessEqual(len(record["stats"]), 7 + 53)
        self.assertIsNotNone(record["views_7d"])

    def test_growth_window_cut_by_next_post(self):
        first = {"channel": "tg", "sent": T0, "members_at_publish": 100}
        second = {"channel": "tg", "sent": T0 + 6 * H, "members_at_publish": 110,
                  "stats": [snap(T0 + 30 * H, members=150)]}  # fmt: skip
        facts = stats.Facts([first, second])
        got = facts.growth(first)
        self.assertEqual((got["growth_24h_attr"], got["growth_window_h"], got["growth_trend"]), (10.0, 6.0, False))
        # У второго окно — полные сутки, до T0 + 30ч, где как раз есть снимок: 110 → 150.
        self.assertEqual(facts.growth(second)["growth_24h"], 40.0)

    def test_growth_minus_channel_trend(self):
        # Неделя без постов: +2 подписчика в сутки — это фон, а не заслуга поста.
        history = [snap(T0 - k * D, members=100 - 2 * k) for k in range(7, 0, -1)]
        old = {"channel": "tg", "sent": T0 - 30 * D, "members_at_publish": 50, "stats": history}
        post = {"channel": "tg", "sent": T0, "members_at_publish": 100, "stats": [snap(T0 + D, members=112)]}
        got = stats.Facts([old, post]).growth(post)
        self.assertEqual((got["growth_24h"], got["growth_24h_attr"], got["growth_trend"]), (12.0, 10.0, True))
        # Окно не закрыто — снимка после суток нет.
        fresh = {"channel": "tg", "sent": T0 + 2 * D, "members_at_publish": 112}
        self.assertIsNone(stats.Facts([old, post, fresh]).growth(fresh)["growth_24h_attr"])

    def test_summary_only_closed_windows_and_old_post_does_not_win(self):
        now = T0 + 30 * D
        old = {"channel": "tg", "sent": T0, "stats": [snap(T0 + 24 * H, 100), snap(now, 5000)]}
        fresh = {"channel": "tg", "sent": now - 25 * H, "stats": [snap(now, 400)]}
        open_ = {"channel": "tg", "sent": now - 2 * H, "stats": [snap(now, 900)]}
        facts = stats.Facts([old, fresh, open_])

        def item(label, record):
            return {"scenario_label": label, "published": [stats.reach(record, facts, now=now)]}

        items = [item("Старый", old), item("Свежий", fresh), item("Час назад", open_)]
        rows = {d["dimension"]: d["rows"] for d in stats.summary(items)}["Сценарий"]
        self.assertEqual([(r["label"], r["median_views"]) for r in rows], [("Свежий", 384), ("Старый", 100)])
        self.assertEqual(items[0]["published"][0]["views"], 5000, "сырые последние просмотры остаются в API")

    def test_summary_counts_channel_once(self):
        # Повтор в тот же канал (AN-005): в «Что работает» — только первая вышедшая публикация.
        first = {"channel": "tg", "state": "sent", "views_24h": 100}
        again = {"channel": "tg", "state": "sent", "views_24h": 900, "republish": True}
        other = {"channel": "tg2", "state": "sent", "views_24h": 300}
        failed = {"channel": "tg3", "state": "failed", "views_24h": 5}
        items = [{"scenario_label": "Статья", "published": [first, again, other, failed]}]
        row = {d["dimension"]: d["rows"] for d in stats.summary(items)}["Сценарий"][0]
        self.assertEqual((row["posts"], row["median_views"]), (2, 200))


class RobustSummaryTest(unittest.TestCase):
    """«Что работает» (AN-007): медиана и квартили вместо среднего, n, покрытие, индекс к каналу."""

    @staticmethod
    def post(views, channel="tg", at=T0, **extra):
        return {"channel": channel, "state": "sent", "sent": at, "views_24h": views, **extra}

    def rows(self, items):
        return {d["dimension"]: d["rows"] for d in stats.summary(items)}["Сценарий"]

    def test_quartiles(self):
        self.assertEqual(stats.quartiles([5]), (5, 5, 5))
        self.assertEqual(stats.quartiles([1, 2, 3, 4, 5]), (2, 3, 4))
        self.assertEqual(stats.quartiles([10, 20, 30, 40]), (17.5, 25, 32.5))

    def test_median_quartiles_n_and_few(self):
        items = [
            {"scenario_label": "Статья", "published": [self.post(v, channel=f"c{v}")]} for v in (10, 20, 30, 40, 50)
        ]
        items.append({"scenario_label": "Пост", "published": [self.post(7)]})
        rows = self.rows(items)
        article = rows[0]
        self.assertEqual(
            (article["label"], article["posts"], article["median_views"], article["p25_views"], article["p75_views"]),
            ("Статья", 5, 30, 20, 40),
        )
        self.assertFalse(article["few"], "5 постов — уже не «мало данных»")
        self.assertTrue(rows[1]["few"])

    def test_outlier_does_not_flip_order(self):
        # Среднее: «Пост» (1000+10+10)/3=340 > «Статья» 100 — один вирусный пост переворачивал порядок.
        items = [
            {
                "scenario_label": "Статья",
                "published": [self.post(100, channel="a"), self.post(100, channel="b"), self.post(100, channel="c")],
            },
            {
                "scenario_label": "Пост",
                "published": [self.post(1000, channel="a"), self.post(10, channel="b"), self.post(10, channel="c")],
            },
        ]
        self.assertEqual([r["label"] for r in self.rows(items)], ["Статья", "Пост"])

    def test_index_to_channel_median(self):
        # Канал big: медиана 1000, пост 2000 → ×2; канал small: медиана 10, пост 20 → ×2.
        # Пост старше 30 дней в базу канала не входит.
        items = [
            {"scenario_label": "Старое", "published": [self.post(1, channel="small", at=T0 - 40 * D)]},
            {
                "scenario_label": "База",
                "published": [self.post(1000, channel="big", at=T0 - D), self.post(10, channel="small", at=T0 - D)],
            },
            {
                "scenario_label": "Новое",
                "published": [self.post(2000, channel="big", at=T0), self.post(20, channel="small", at=T0)],
            },
        ]
        rows = {r["label"]: r for r in self.rows(items)}
        self.assertEqual(rows["Новое"]["median_index"], 1.33, "медиана канала за 30 дней включает сам пост: 2000/1500")
        self.assertEqual(rows["База"]["median_index"], 1.0)
        self.assertEqual(rows["Старое"]["median_index"], 1.0)

    def test_coverage(self):
        items = [
            {
                "published": [
                    self.post(100),
                    self.post(None, channel="fresh", age_h=3),
                    self.post(None, channel="private", age_h=50),
                    {"channel": "yt", "state": "packaged"},
                ]
            }
        ]
        self.assertEqual(stats.coverage(items), {"counted": 1, "total": 3, "fresh": 1, "no_data": 1})


class WeekTest(unittest.TestCase):
    """Плашка недели (AN-016) на фикстурах."""

    def test_four_numbers(self):
        now = T0 + 30 * D
        projects = [
            {"published_at": now - 20 * H, "ready_at": now - 30 * H},  # вышел, 10 ч до публикации
            {"published_at": now - 6 * H, "ready_at": now - 10 * H},  # вышел, 4 ч
            {"published_at": now - 3 * H},  # вышел, когда готово видео — неизвестно
            {"published_at": now - 10 * D, "ready_at": now - 11 * D},  # вне недели
            {"waiting": True, "updated_at": now - H},  # ждёт
            {"waiting": True, "updated_at": now - 100 * H},  # брошен
            {"waiting": False, "updated_at": now - 200 * H},  # черновик
        ]
        self.assertEqual(
            stats.week(projects, now=now),
            {"published": 3, "waiting": 1, "abandoned": 1, "median_lead_h": 7.0, "days": 7},
        )
        self.assertIsNone(stats.week([], now=now)["median_lead_h"])


class VariantsTest(unittest.TestCase):
    """Варианты выпуска (AN-019): доля переделок и копии одного оригинала рядом с n и медианой."""

    @staticmethod
    def item(pid, views, variant_of=None, variant=None):
        published = [
            {"channel": f"c{i}", "state": "sent", "views_24h": v, "growth_24h_attr": 1} for i, v in enumerate(views)
        ]
        return {
            "id": pid,
            "title": f"Выпуск {pid}",
            "variant_of": variant_of,
            "variant": variant,
            "published": published,
        }

    def test_aggregate(self):
        items = [
            self.item("a", [100, 300]),
            self.item("b", [500], variant_of="a", variant="другая обложка"),
            self.item("c", [], variant_of="a"),
            self.item("d", [50]),
            self.item("a", [100, 300]),  # тот же выпуск из второй выборки — один раз
        ]
        got = stats.variants(items)
        self.assertEqual((got["projects"], got["copies"], got["share"]), (4, 2, 0.5))
        [group] = got["groups"]
        self.assertEqual((group["root"], group["title"]), ("a", "Выпуск a"))
        rows = [(r["id"], r["variant"], r["posts"], r["median_views"], r["few"]) for r in group["rows"]]
        self.assertEqual(
            rows,
            [
                ("a", "оригинал", 2, 200, True),
                ("b", "другая обложка", 1, 500, True),
                ("c", "без описания", 0, None, True),
            ],
        )

    def test_no_copies(self):
        self.assertEqual(stats.variants([self.item("a", [1])])["groups"], [])
        self.assertIsNone(stats.variants([])["share"])


class StatsHttpTest(StudioCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        channels = [
            {"id": "tg", "label": "TG", "type": "telegram", "chat_id": "-100123", "token_env": "AIP_TG_TEST"},
            {"id": "yt", "label": "YouTube", "type": "manual"},
        ]
        (folder / "channels.json").write_text(json.dumps(channels), encoding="utf-8")
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(folder / "channels.json")}
        super().setUp()

    def test_refresh_manual_views_and_summary(self):
        project = self.create(scenario="article", text=ARTICLE)
        pid = project["id"]
        published = [
            {"channel": "tg", "ok": True, "message_id": 7, "members_at_publish": 100, "sent": time.time() - 30 * 3600},
            {"channel": "yt", "ok": True, "files": []},
        ]
        (self.folder(pid) / "published.json").write_text(json.dumps(published), encoding="utf-8")

        tg = FakeTelegram(members=150, chat={"username": "mychan"}).start()
        self.addCleanup(tg.stop)
        with (
            mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN, **tg.env()}),
            mock.patch.object(stats, "http_get", lambda url: EMBED),
        ):
            status, _ = self.json("POST", "/api/studio/stats/refresh")
            self.assertEqual(status, 202)
            board = self.poll("/api/studio/board", lambda b: not b["stats"]["running"] and b["stats"]["at"])
        tg = next(r for r in board["projects"][0]["published"] if r["channel"] == "tg")
        self.assertEqual((tg["views"], tg["growth"], tg["public"]), (1200, 50, True))
        # Окно 24ч: снимок на 30-м часу — интерполяция от нуля в момент публикации; прирост — к концу суток.
        self.assertEqual((tg["views_24h"], tg["views_72h"]), (960, None))
        self.assertEqual((tg["growth_24h_attr"], tg["growth_trend"]), (40.0, False))

        status, view = self.json("POST", f"/api/studio/projects/{pid}/stats", {"channel": "yt", "views": 300})
        self.assertEqual(status, 200, view)
        for bad in ({"channel": "yt", "views": -1}, {"channel": "nope", "views": 1}, {"channel": "yt", "views": "10"}):
            self.assertEqual(self.json("POST", f"/api/studio/projects/{pid}/stats", bad)[0], 400, bad)
        board = self.json("GET", "/api/studio/board")[1]
        rows = {d["dimension"]: d["rows"] for d in board["response"]}
        self.assertEqual(
            [(r["label"], r["posts"], r["median_views"], r["median_growth"]) for r in rows["Сценарий"]],
            [("Видео по статье", 1, 960, 40.0)],
        )
        # Покрытие (AN-007): TG учтён; пакет YouTube без «Залил» — не публикация и в знаменатель не входит.
        self.assertEqual((board["coverage"]["counted"], board["coverage"]["total"]), (1, 1))
        saved = steps.load_published(self.folder(pid))
        self.assertTrue(saved[1]["stats"][-1]["manual"])

    def test_manual_metrics_with_measure_date(self):
        """Ручной ввод (AN-010): метрики YouTube Studio и дата замера в прошлом → верный age_h."""
        pid = self.create(scenario="article", text=ARTICLE)["id"]
        published_at = time.time() - 5 * 86400
        record = {"channel": "yt", "ok": True, "state": "uploaded", "published_at": published_at, "files": []}
        (self.folder(pid) / "published.json").write_text(json.dumps([record]), encoding="utf-8")
        measured = published_at + 48 * 3600
        body = {"channel": "yt", "views": 500, "impressions": 4000, "ctr": 6.5, "avg_view_pct": 41, "subs_gained": 3}
        status, view = self.json("POST", f"/api/studio/projects/{pid}/stats", body | {"at": measured})
        self.assertEqual(status, 200, view)
        # Снимок «сейчас» без даты — после него, порядок истории по времени замера.
        self.assertEqual(
            self.json("POST", f"/api/studio/projects/{pid}/stats", {"channel": "yt", "views": 900})[0], 200
        )
        # ISO-строка из формы (datetime-local, местное время) — раньше: встаёт первой.
        early = time.strftime("%Y-%m-%dT%H:%M", time.localtime(published_at + 12 * 3600))
        self.assertEqual(
            self.json("POST", f"/api/studio/projects/{pid}/stats", {"channel": "yt", "views": 200, "at": early})[0], 200
        )
        saved = steps.load_published(self.folder(pid))[0]
        snaps = saved["stats"]
        self.assertEqual([s["views"] for s in snaps], [200, 500, 900])
        self.assertAlmostEqual(snaps[1]["age_h"], 48.0, places=2)
        self.assertAlmostEqual(snaps[0]["age_h"], 12.0, delta=1 / 60)
        self.assertEqual((snaps[1]["ctr"], snaps[1]["subs_gained"], snaps[1]["manual"]), (6.5, 3, True))
        # Окна — по дате замера: первый замер (48ч) закрыл окно 24ч от нуля в момент публикации — 250;
        # закрытое окно заморожено, поздний ввод более раннего замера его не сдвигает (AN-001).
        self.assertEqual((saved["views_24h"], saved["views_72h"]), (250, 633), "72ч: 500 + 400·24/72")

        board = self.json("GET", "/api/studio/board")[1]
        yt = board["projects"][0]["published"][0]
        self.assertEqual(
            yt["manual"],
            {"views": 500, "impressions": 4000, "ctr": 6.5, "avg_view_pct": 41.0, "subs_gained": 3, "at": measured},
        )
        self.assertEqual(yt["views"], 900, "последние просмотры — по времени замера")

    def test_manual_metrics_validation(self):
        pid = self.create(scenario="article", text=ARTICLE)["id"]
        record = {"channel": "yt", "ok": True, "state": "uploaded", "published_at": time.time() - 3600, "files": []}
        (self.folder(pid) / "published.json").write_text(json.dumps([record]), encoding="utf-8")
        url = f"/api/studio/projects/{pid}/stats"
        cases = [
            ({"channel": "yt", "ctr": 120}, "CTR обложки"),
            ({"channel": "yt", "avg_view_pct": -1}, "% досмотра"),
            ({"channel": "yt", "impressions": 1.5}, "Показы"),
            ({"channel": "yt"}, "хотя бы одну"),
            ({"channel": "yt", "views": 1, "at": "вчера"}, "Дата замера"),
            ({"channel": "yt", "views": 1, "at": time.time() + 86400}, "в будущем"),
            ({"channel": "yt", "views": 1, "at": time.time() - 86400}, "раньше публикации"),
        ]
        for body, text in cases:
            status, err = self.json("POST", url, body)
            self.assertEqual(status, 400, body)
            self.assertIn(text, json.dumps(err, ensure_ascii=False), body)
        self.assertEqual(
            self.json("POST", url, {"channel": "yt", "subs_gained": -2})[0], 200, "отписки с ролика — минус"
        )


class WeekHttpTest(StudioCase):
    """Плашка недели на доске (AN-016): из project.json и published.json, пакет без ссылки — не публикация."""

    def project(self, *, ready_at=None, updated_at=None, records=None, archived=False):
        self.made = getattr(self, "made", 0) + 1  # свой текст и force_new — иначе 409 «уже есть выпуск»
        pid = self.create(scenario="article", text=f"{ARTICLE}\n\nВыпуск {self.made}.", force_new=True)["id"]
        self.settle(pid)
        studio = self.app.modules["studio"].studio
        with studio._lock:
            project = studio._projects[pid]
            render = project["steps"]["render"]
            if ready_at is not None:
                render.update(status="done", updated_at=ready_at)
            else:
                render.update(status="pending")
            project["archived"] = archived
            project["updated_at"] = updated_at or time.time()
        if records is not None:
            (self.folder(pid) / "published.json").write_text(json.dumps(records), encoding="utf-8")
        return pid

    def test_week_on_board(self):
        now = time.time()
        sent = {"channel": "tg", "ok": True, "state": "sent", "sent": now - 20 * 3600, "message_id": 1}
        self.project(ready_at=now - 30 * 3600, records=[sent])
        uploaded = {"channel": "yt", "ok": True, "state": "uploaded", "published_at": now - 6 * 3600, "files": ["v"]}
        self.project(ready_at=now - 10 * 3600, records=[uploaded], archived=True)
        package = {"channel": "yt", "ok": True, "files": ["publish/yt/video.mp4"]}
        self.project(ready_at=now - 5 * 3600, records=[package])
        self.project(ready_at=now - 200 * 3600, updated_at=now - 100 * 3600)
        self.project()  # черновик: видео нет
        week = self.json("GET", "/api/studio/board")[1]["week"]
        self.assertEqual(
            (week["published"], week["waiting"], week["abandoned"], week["median_lead_h"]), (2, 1, 1, 7.0), week
        )


class VariantsHttpTest(StudioCase):
    def test_clone_with_variant_shows_on_board(self):
        """Копия с «что меняли» (AN-019): variant сохранён, на доске — группа вариантов оригинала."""
        source = self.create(scenario="article", text=ARTICLE)
        status, copy = self.json("POST", f"/api/studio/projects/{source['id']}/clone", {"variant": "короче на минуту"})
        self.assertEqual(status, 201, copy)
        self.assertEqual(copy["variant"], "короче на минуту")
        board = self.json("GET", "/api/studio/board")[1]
        [group] = board["variants"]["groups"]
        self.assertEqual(group["root"], source["id"])
        self.assertEqual([r["variant"] for r in group["rows"]], ["оригинал", "короче на минуту"])
        self.assertEqual((board["variants"]["copies"], board["variants"]["projects"]), (1, 2))


if __name__ == "__main__":
    unittest.main()
