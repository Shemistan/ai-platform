"""Доска «Выпуски»: группы, что ждёт человека, публикация с доски, архив."""

import unittest

from aiplatform.studio import pipeline
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
        self.assertEqual(item["channels"], [{"id": "download-16x9", "label": "Файл для YouTube / VK (16:9)"}])
        self.assertTrue(item["poster"].startswith(f"/api/studio/projects/{project['id']}/files/poster.png"))
        self.assertEqual(board["counts"]["attention"], 1)
        _, status = self.json("GET", "/api/status")
        self.assertEqual(status["studio_attention"], 1)

        # Публикация с доски — тем же запросом, что и кнопка шага.
        status, _ = self.json("POST", f"/api/studio/projects/{project['id']}/publish", {"channels": ["download-16x9"]})
        self.assertEqual(status, 200)
        self.settle(project["id"])
        item = next(p for p in self.board()["projects"] if p["id"] == project["id"])
        self.assertEqual(item["group"], "published")
        self.assertEqual([r["channel"] for r in item["published"]], ["download-16x9"])
        self.assertIsNone(item["attention"])
        self.assertEqual(self.json("GET", "/api/status")[1]["studio_attention"], 0)

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
