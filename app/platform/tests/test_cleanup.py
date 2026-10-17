"""Уборка: место по областям, удаление старых задач вкладок, кэша, загрузок и архивных выпусков,
промежуточные файлы и авто-архив опубликованных выпусков (REL-013)."""

import json
import os
import time
from unittest import mock

from aiplatform.studio import history
from tests.fixtures import ARTICLE, NEEDS_FFMPEG
from tests.helpers import StudioCase

DAY = 86400
MONTH_LATER = 31 * DAY
POLICY = {"trim_days": 14, "archive_days": 60}


@NEEDS_FFMPEG
class CleanupTest(StudioCase):
    @property
    def cleaner(self):
        return self.app.modules["settings"].cleaner

    def test_only_recoverable_old_data_is_removed(self):
        project = self.create(scenario="article", text=ARTICLE, run=True)
        self.settle(project["id"])
        status, body = self.json("GET", "/api/settings/cleanup")
        self.assertEqual(status, 200, body)
        fresh = {"jobs": 0, "cache": 0, "archived": 0, "uploads": 0, "trimmed": 0, "auto_archived": 0, "bytes": 0}
        self.assertEqual(body["found"], fresh)  # всё свежее
        usage = {u["id"]: u["bytes"] for u in body["usage"]}
        self.assertGreater(usage["photo"], 0)
        self.assertGreater(usage["cache"], 0)
        self.assertEqual(usage["archived"], 0)

        later = time.time() + MONTH_LATER
        preview = self.cleaner.run(30, dry=True, now=later)
        self.assertEqual((preview["jobs"], preview["cache"], preview["archived"]), (6, 6, 0))  # 5 фото + озвучка

        # Неархивный выпуск остаётся даже через месяц, его файлы целы.
        self.cleaner.run(30, dry=False, now=later)
        self.assertEqual(self.json("GET", "/api/photo/jobs")[1]["jobs"], [])
        self.assertEqual(self.json("GET", "/api/audio/jobs")[1]["jobs"], [])
        view = self.json("GET", f"/api/studio/projects/{project['id']}")[1]
        self.assertTrue((self.folder(project["id"]) / "video.mp4").is_file())
        self.assertEqual(self.call("GET", view["files"]["poster"])[0], 200)

        self.json("POST", f"/api/studio/projects/{project['id']}/archive", {"archived": True})
        self.assertEqual(self.cleaner.run(30, dry=True, now=time.time())["archived"], 0)  # архивный, но свежий
        result = self.cleaner.run(30, dry=False, now=later)
        self.assertEqual(result["archived"], 1)
        self.assertFalse(self.folder(project["id"]).exists())
        self.assertEqual(self.json("GET", f"/api/studio/projects/{project['id']}")[0], 404)

    def test_settings_and_run_endpoint(self):
        status, body = self.json("POST", "/api/settings/section/cleanup", {"days": 45})
        self.assertEqual(status, 400, body)
        status, body = self.json("POST", "/api/settings/section/cleanup", {"days": 14, "auto": True})
        self.assertEqual((status, body["cleanup"]["days"], body["cleanup"]["auto"]), (200, 14, True))
        status, body = self.json("POST", "/api/settings/cleanup/run")
        self.assertEqual((status, body["done"], body["days"]), (200, True, 14))

    def test_published_project_trimmed_then_auto_archived(self):
        settings = {"shorts": True, "channels": ["download-16x9"]}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        self.settle(project["id"])
        self.json("POST", f"/api/studio/projects/{project['id']}/approve", {"step": "publish"})
        self.assertEqual(self.statuses(self.settle(project["id"]))["publish"], "done")
        folder = self.folder(project["id"])
        self.assertTrue((folder / "images").is_dir() and (folder / "short").is_dir())
        kept = ["video.mp4", "short.mp4", "project.json", "published.json", "text.md", "plan.json"]
        self.assertTrue(all((folder / name).is_file() for name in kept))

        # Раньше trim_days — ничего; после — без картинок слайдов и файлов шортса.
        soon = self.cleaner.run(30, dry=False, now=time.time() + 5 * DAY, policy=POLICY)
        self.assertEqual((soon["trimmed"], soon["auto_archived"]), (0, 0))
        preview = self.cleaner.run(30, dry=True, now=time.time() + 15 * DAY, policy=POLICY)
        self.assertEqual(preview["trimmed"], 1)
        self.assertTrue((folder / "images").is_dir(), "dry ничего не удаляет")
        result = self.cleaner.run(30, dry=False, now=time.time() + 15 * DAY, policy=POLICY)
        self.assertEqual((result["trimmed"], result["auto_archived"]), (1, 0))
        self.assertGreater(result["bytes"], 0)
        self.assertFalse((folder / "images").exists())
        self.assertFalse((folder / "short").exists())
        self.assertTrue(all((folder / name).is_file() for name in kept), sorted(p.name for p in folder.iterdir()))
        self.assertEqual(self.cleaner.run(30, dry=False, now=time.time() + 16 * DAY, policy=POLICY)["trimmed"], 0)

        # Через archive_days — в архив сам; потом уборка архива его не удаляет.
        later = self.cleaner.run(30, dry=False, now=time.time() + 61 * DAY, policy=POLICY)
        self.assertEqual(later["auto_archived"], 1)
        view = self.json("GET", f"/api/studio/projects/{project['id']}")[1]
        self.assertTrue(view["archived"])
        much_later = self.cleaner.run(30, dry=False, now=time.time() + 200 * DAY, policy=POLICY)
        self.assertEqual(much_later["archived"], 0)
        self.assertTrue((folder / "published.json").is_file())

    def test_old_uploads_removed(self):
        uploads = self.app.ctx.uploads.root
        old, new = uploads / ("a" * 32 + ".png"), uploads / ("b" * 32 + ".png")
        for path in (old, new):
            path.write_bytes(b"x" * 100)
        stamp = time.time() - MONTH_LATER
        os.utime(old, (stamp, stamp))
        result = self.cleaner.run(30, dry=False, policy=POLICY)
        self.assertEqual(result["uploads"], 1)
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_old_upload_used_by_live_job_is_kept(self):
        # CMP-016: старая загрузка, на которую ссылается оставшаяся задача, — не удаляется.
        uploads = self.app.ctx.uploads.root
        used, unused = uploads / ("c" * 32 + ".png"), uploads / ("d" * 32 + ".jpg")
        stamp = time.time() - MONTH_LATER
        for path in (used, unused):
            path.write_bytes(b"x" * 100)
            os.utime(path, (stamp, stamp))
        job = {"id": "j1", "status": "done", "finished_at": time.time(), "source_url": f"/api/photo/uploads/{'c' * 32}"}
        with mock.patch.object(self.app.modules["photo"].jobs, "list", return_value=[job]):
            dry = self.cleaner.run(30, dry=True, policy=POLICY)
            self.assertEqual(dry["uploads"], 1)
            result = self.cleaner.run(30, dry=False, policy=POLICY)
        self.assertEqual(result["uploads"], 1)
        self.assertTrue(used.exists(), "загрузку использует живая задача")
        self.assertFalse(unused.exists())

    def test_auto_cleanup_on_by_default(self):
        self.assertIs(self.json("GET", "/api/settings/state")[1]["cleanup"]["auto"], True)
        self.assertEqual(self.json("GET", "/api/settings/state")[1]["cleanup"]["days"], 30)


class CleanupKeepsPublicationsTest(StudioCase):
    """Уборка архивного выпуска: медиа удалены, публикации и снимки — в архиве аналитики (AN-008)."""

    def test_publications_survive_cleanup(self):
        pid = self.create(scenario="article", text=ARTICLE)["id"]
        folder = self.folder(pid)
        (folder / "video.mp4").write_bytes(b"x" * 50_000)
        sent = time.time() - 3 * 86400
        record = {
            "channel": "tg-old",
            "ok": True,
            "state": "sent",
            "sent": sent,
            "message_id": 7,
            "members_at_publish": 100,
            "stats": [{"at": sent + 30 * 3600, "age_h": 30.0, "views": 300, "members": 120}],
        }
        (folder / "published.json").write_text(json.dumps([record]), encoding="utf-8")
        self.json("POST", f"/api/studio/projects/{pid}/archive", {"archived": True})
        cleaner = self.app.modules["settings"].cleaner
        later = time.time() + MONTH_LATER

        result = cleaner.run(30, dry=False, now=later)
        self.assertEqual(result["archived"], 1)
        self.assertGreaterEqual(result["bytes"], 50_000, "место освобождено")
        self.assertFalse(folder.exists(), "медиа удалены вместе с выпуском")

        studio = self.app.modules["studio"]
        kept = history.load(studio.history_file)
        self.assertEqual([k["project_id"] for k in kept], [pid])
        self.assertEqual(kept[0]["published"][0]["stats"], record["stats"], "снимки отклика целы")
        self.assertNotIn("log", kept[0]["project"])

        board = self.json("GET", "/api/studio/board")[1]
        rows = {d["dimension"]: d["rows"] for d in board["response"]}["Сценарий"]
        self.assertEqual([(r["posts"], r["avg_views"]) for r in rows], [(1, 240)], "«Что работает» учитывает удалённый")
        self.assertIn("tg-old", studio._facts().posts, "прирост канала — и по удалённым выпускам")

        # Повторная запись того же выпуска не дублируется.
        self.assertFalse(history.keep(studio.history_file, kept[0]["project"], kept[0]["published"]))
        cleaner.run(30, dry=False, now=later)
        self.assertEqual(len(history.load(studio.history_file)), 1)
