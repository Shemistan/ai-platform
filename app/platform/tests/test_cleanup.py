"""Уборка: место по областям, удаление старых задач вкладок, кэша и архивных выпусков."""

import time

from tests.test_studio_flow import ARTICLE, NEEDS_FFMPEG, StudioCase

MONTH_LATER = 31 * 86400


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
        self.assertEqual(body["found"], {"jobs": 0, "cache": 0, "archived": 0, "bytes": 0})  # всё свежее
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
