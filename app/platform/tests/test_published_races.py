"""published.json без гонок (BE-002): отклик и публикация пишут журнал под одним замком выпуска."""

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.studio import stats, steps
from tests.test_studio_flow import ARTICLE, StudioCase


class UpdatePublishedTest(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir)

    def test_parallel_updates_lose_nothing(self):
        def writer(prefix):
            for n in range(40):
                steps.update_published(self.dir, lambda rs, c=f"{prefix}{n}": [*rs, {"channel": c, "ok": True}])

        threads = [threading.Thread(target=writer, args=(p,)) for p in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        channels = {r["channel"] for r in steps.load_published(self.dir)}
        self.assertEqual(len(channels), 80)
        self.assertEqual([p.name for p in self.dir.iterdir()], [steps.PUBLISHED], "tmp не остались")


class RefreshDuringPublishTest(StudioCase):
    def test_refresh_keeps_record_written_meanwhile(self):
        project = self.create(scenario="article", text=ARTICLE)
        folder = self.folder(project["id"])
        first = {"channel": "tg-old", "ok": True, "message_id": 5, "chat_id": "@old_chan", "state": "sent"}
        (folder / steps.PUBLISHED).write_text(json.dumps([first]), encoding="utf-8")
        new_channel = {"channel": "tg-new", "ok": True, "message_id": 9, "state": "sent"}

        def slow_record(self_, record):
            # Пока отклик ходит в сеть, шаг публикации дописывает журнал новым каналом.
            steps.update_published(folder, lambda rs: [*rs, new_channel])
            return stats.add_snapshot(record, {"views": 100, "members": 10})

        with mock.patch.object(stats.Refresher, "record", slow_record):
            self.app.modules["studio"].refresh_stats_now()
        records = {r["channel"]: r for r in steps.load_published(folder)}
        self.assertIn("tg-new", records, "запись о новом канале не затёрта")
        self.assertEqual(records["tg-old"]["stats"][-1]["views"], 100, "снимок отклика дописан")

    def test_manual_stats_keeps_journal(self):
        project = self.create(scenario="article", text=ARTICLE)
        folder = self.folder(project["id"])
        steps.save_published(folder, [{"channel": "pkg", "ok": True, "state": "uploaded"}])
        status, _ = self.json("POST", f"/api/studio/projects/{project['id']}/stats", {"channel": "pkg", "views": 7})
        self.assertEqual(status, 200)
        self.assertEqual(steps.load_published(folder)[0]["stats"][-1]["views"], 7)


if __name__ == "__main__":
    unittest.main()
