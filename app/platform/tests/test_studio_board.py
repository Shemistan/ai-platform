"""Доска «Выпуски»: группы, что ждёт человека, публикация с доски, архив."""

import time
import unittest
from pathlib import Path

from aiplatform.core import events
from aiplatform.studio import pipeline, publish, service
from tests.test_studio_flow import ARTICLE, NEEDS_FFMPEG, StudioCase


def project_with(statuses: dict, running=False) -> dict:
    steps = {s: {"status": "done", "note": None, "error": None} for s in pipeline.STEP_IDS}
    for step, status in statuses.items():
        steps[step] |= status if isinstance(status, dict) else {"status": status}
    return {"running": running, "steps": steps}


class AttentionTest(unittest.TestCase):
    def test_first_waiting_or_failed_step(self):
        project = project_with(
            {"poster": {"status": "waiting", "note": "Загрузи постер"}, "publish": {"status": "failed", "error": "x"}}
        )
        self.assertEqual(
            pipeline.attention(project),
            {"step": "poster", "label": "Постер — первый кадр", "status": "waiting", "text": "Загрузи постер"},
        )
        failed = project_with({"render": {"status": "failed", "error": "нет ffmpeg"}})
        self.assertEqual(pipeline.attention(failed)["text"], "нет ffmpeg")

    def test_nothing_while_running_or_all_done(self):
        self.assertIsNone(pipeline.attention(project_with({"poster": "waiting"}, running=True)))
        self.assertIsNone(pipeline.attention(project_with({})))


class BoardGroupTest(unittest.TestCase):
    """Группы доски по журналу публикации (AN-003): опубликовано — только то, что вышло."""

    item = {"running": False, "attention": None}

    def group(self, records, render="done", publish_status="done"):
        project = project_with({"render": render, "publish": publish_status})
        return service.board_group(self.item, project, records)

    def test_groups(self):
        legacy_package = {"channel": "download-16x9", "ok": True, "files": ["publish/download-16x9/video.mp4"]}
        self.assertEqual(self.group([legacy_package]), "uploaded", "пакет без ссылки — не публикация")
        uploaded = legacy_package | {"state": "uploaded", "external_url": "https://youtu.be/x", "published_at": 1.0}
        self.assertEqual(self.group([uploaded]), "published")
        self.assertEqual(self.group([{"channel": "tg", "ok": True, "state": "sent", "sent": 1.0}]), "published")
        self.assertEqual(self.group([{"channel": "tg", "ok": True, "sent": 1.0, "message_id": 5}]), "published")
        self.assertEqual(self.group([], publish_status="pending"), "attention", "видео готово — ждёт тебя")
        self.assertEqual(self.group([], render="pending", publish_status="pending"), "drafts")
        failed = {"channel": "tg", "ok": False, "state": "failed"}
        self.assertEqual(self.group([failed], publish_status="pending"), "attention")

    def test_external_video(self):
        cases = {
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=3": ("youtube", "dQw4w9WgXcQ"),
            "https://youtu.be/dQw4w9WgXcQ": ("youtube", "dQw4w9WgXcQ"),
            "https://youtube.com/shorts/dQw4w9WgXcQ?feature=share": ("youtube", "dQw4w9WgXcQ"),
            "https://www.tiktok.com/@me/video/7300000000000000000": ("tiktok", "7300000000000000000"),
            "https://vm.tiktok.com/ZMabcdef/": ("tiktok", None),
            "https://vk.com/video-1_2": ("other", None),
        }
        for url, (platform, vid) in cases.items():
            got = publish.external_video(url)
            self.assertEqual((got["platform"], got["external_id"], got["external_url"]), (platform, vid, url), url)
        for bad in ("", None, "javascript:alert(1)", "ftp://youtube.com/x", "https://youtube.com/watch", "просто"):
            with self.assertRaises(ValueError, msg=bad):
                publish.external_video(bad)


@NEEDS_FFMPEG
class BoardTest(StudioCase):
    def board(self, archived=False):
        status, body = self.json("GET", "/api/studio/board" + ("?archived=1" if archived else ""))
        self.assertEqual(status, 200, body)
        return body

    def test_waiting_project_publish_from_board_and_archive(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"channels": ["download-16x9"]}, run=True)
        self.settle(project["id"])
        board = self.board()
        item = next(p for p in board["projects"] if p["id"] == project["id"])
        self.assertEqual(item["group"], "attention")
        self.assertEqual(item["attention"]["step"], "publish")
        self.assertEqual(
            item["channels"], [{"id": "download-16x9", "label": "Файл для YouTube / VK (16:9)", "type": "manual"}]
        )
        # Для подтверждения публикации с доски — начало подписи.
        self.assertTrue(item["caption"])
        self.assertLessEqual(len(item["caption"]), 300)
        # Перезапуск шага в студии показывает, что сбросится.
        _, view = self.json("GET", f"/api/studio/projects/{project['id']}")
        resets = {s["id"]: s["dependents"] for s in view["steps"]}
        self.assertEqual(resets["voice"], ["render", "publish"])
        self.assertEqual(resets["publish"], [])
        self.assertTrue(item["poster"].startswith(f"/api/studio/projects/{project['id']}/files/poster.png"))
        self.assertEqual(board["counts"]["attention"], 1)
        _, status = self.json("GET", "/api/status")
        self.assertEqual(status["studio_attention"], 1)

        # Публикация с доски — тем же запросом, что и кнопка шага.
        status, _ = self.json("POST", f"/api/studio/projects/{project['id']}/publish", {"channels": ["download-16x9"]})
        self.assertEqual(status, 200)
        self.settle(project["id"])
        item = next(p for p in self.board()["projects"] if p["id"] == project["id"])
        # Пакет для ручной загрузки — ещё не публикация: файл до YouTube мог не дойти (AN-003).
        self.assertEqual(item["group"], "uploaded")
        self.assertEqual([(r["channel"], r["state"]) for r in item["published"]], [("download-16x9", "packaged")])
        self.assertIsNone(item["attention"])
        self.assertEqual(self.json("GET", "/api/status")[1]["studio_attention"], 0)

        # «Залил» со ссылкой на ролик — теперь опубликован.
        url = f"/api/studio/projects/{project['id']}/uploaded"
        for bad in (
            {"channel": "download-16x9", "url": "ftp://x"},
            {"channel": "nope", "url": "https://youtu.be/dQw4w9WgXcQ"},
        ):
            self.assertEqual(self.json("POST", url, bad)[0], 400, bad)
        status, view = self.json("POST", url, {"channel": "download-16x9", "url": "https://youtu.be/dQw4w9WgXcQ"})
        self.assertEqual(status, 200, view)
        record = view["published"][0]
        self.assertEqual((record["state"], record["external_id"]), ("uploaded", "dQw4w9WgXcQ"))
        self.assertEqual(record["external_url"], "https://youtu.be/dQw4w9WgXcQ")
        self.assertAlmostEqual(record["published_at"], time.time(), delta=60)
        item = next(p for p in self.board()["projects"] if p["id"] == project["id"])
        self.assertEqual(item["group"], "published")
        self.assertEqual(item["published"][0]["external_url"], "https://youtu.be/dQw4w9WgXcQ")
        log = events.EventLog(Path(self.tmp.name) / events.FILE).read()
        self.assertEqual([e["event"] for e in log if e["event"].startswith("publication.")][-2:],
                         ["publication.packaged", "publication.uploaded"])  # fmt: skip

        # Архив: пропадает с доски и из списка сценария, виден в архиве, возвращается.
        status, view = self.json("POST", f"/api/studio/projects/{project['id']}/archive", {"archived": True})
        self.assertEqual((status, view["archived"]), (200, True))
        self.assertEqual(self.board()["projects"], [])
        self.assertEqual(self.json("GET", "/api/studio/projects?scenario=article")[1]["projects"], [])
        self.assertEqual([p["id"] for p in self.board(archived=True)["projects"]], [project["id"]])
        self.json("POST", f"/api/studio/projects/{project['id']}/archive", {"archived": False})
        self.assertEqual([p["id"] for p in self.board()["projects"]], [project["id"]])

    def test_draft_and_running_groups(self):
        draft = self.create(scenario="article", text=ARTICLE)
        item = self.board()["projects"][0]
        self.assertEqual((item["id"], item["group"], item["attention"]), (draft["id"], "drafts", None))
        status, body = self.json("POST", f"/api/studio/projects/{draft['id']}/archive", {"archived": "yes"})
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
