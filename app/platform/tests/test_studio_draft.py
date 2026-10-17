"""Черновик и чистовик; длинная статья — серия по главам и выжимка."""

import unittest

from aiplatform.core import media
from aiplatform.studio import series
from tests.fixtures import ARTICLE, NEEDS_FFMPEG
from tests.helpers import StudioCase


def words(n: int, word: str = "слово") -> str:
    sentences = [" ".join([word] * 10) + "." for _ in range(n // 10)]
    return " ".join(sentences)


def chapter(title: str, n: int) -> str:
    return f"# {title}\n\n" + "\n\n".join(words(100) for _ in range(n // 100))


class SeriesTest(unittest.TestCase):
    def test_by_headings_merging_small_chapters(self):
        text = "\n\n".join([chapter("Начало", 300), chapter("Хранилище", 400), chapter("Переезд", 700)])
        parts = series.split(text)
        self.assertEqual([p.title for p in parts], ["Начало", "Переезд"])
        self.assertEqual([p.words for p in parts], [704, 702])  # «# Глава» — тоже слова
        self.assertTrue(parts[0].text.startswith("# Начало"))
        self.assertIn("# Хранилище", parts[0].text)

    def test_long_chapter_by_paragraphs_and_tail(self):
        text = "\n\n".join(words(100) for _ in range(25))  # 2500 слов без заголовков
        parts = series.split(text)
        self.assertEqual(len(parts), 2)
        self.assertTrue(all(1000 <= p.words <= 1500 for p in parts), [p.words for p in parts])
        self.assertEqual(len(series.split(words(300))), 1)


@NEEDS_FFMPEG
class DraftTest(StudioCase):
    def jobs(self, tab):
        return len(self.json("GET", f"/api/{tab}/jobs")[1]["jobs"])

    def test_draft_then_final(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"draft": True}, run=True)
        draft = self.settle(project["id"])
        steps = self.statuses(draft)
        self.assertEqual((steps["images"], steps["render"]), ("skipped", "done"), draft["log"])
        self.assertEqual(self.jobs("photo"), 1)  # только постер
        video = self.folder(project["id"]) / "video.mp4"
        self.assertEqual(media.video_size(video), (240, 136))

        status, final = self.json("POST", f"/api/studio/projects/{project['id']}/settings", {"draft": False})
        self.assertEqual(status, 200, final)
        steps = self.statuses(final)
        self.assertEqual((steps["images"], steps["render"], steps["voice"]), ("pending", "pending", "done"))
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {})
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["images"], "done", done["log"])
        self.assertEqual(media.video_size(video), (480, 270))
        self.assertEqual(self.jobs("audio"), 1)  # озвучка не переделывалась

    def test_split_long_article_into_series(self):
        text = "\n\n".join([chapter("Начало", 700), chapter("Середина", 700), chapter("Конец", 700)])
        source = self.create(scenario="article", text=text, title="Большая статья")
        self.assertTrue(any("серии" in x["text"] for x in source["preflight"]))
        status, body = self.json("POST", f"/api/studio/projects/{source['id']}/split", {})
        self.assertEqual(status, 201, body)
        titles = [p["title"] for p in body["projects"]]
        self.assertEqual(
            titles,
            [
                "Большая статья — часть 1 из 3: Начало",
                "Большая статья — часть 2 из 3: Середина",
                "Большая статья — часть 3 из 3: Конец",
            ],
        )
        first = self.json("GET", f"/api/studio/projects/{body['projects'][0]['id']}")[1]
        self.assertTrue(first["artifacts"]["text"].startswith("# Начало"))
        self.assertTrue(self.json("GET", f"/api/studio/projects/{source['id']}")[1]["archived"])
        short = self.create(scenario="article", text=ARTICLE)
        status, _ = self.json("POST", f"/api/studio/projects/{short['id']}/split", {})
        self.assertEqual(status, 400)

    def test_summary_becomes_new_release(self):
        text = "\n\n".join(f"Абзац {n}: {words(100)}" for n in range(20))
        source = self.create(scenario="article", text=text, title="Длинная")
        status, _ = self.json("POST", f"/api/studio/projects/{source['id']}/summary")
        self.assertEqual(status, 202)
        done = self.poll(
            f"/api/studio/projects/{source['id']}", lambda p: (p.get("summary") or {}).get("status") != "running"
        )
        self.assertEqual(done["summary"]["status"], "done", done)
        summary = self.json("GET", f"/api/studio/projects/{done['summary']['project']}")[1]
        self.assertEqual(summary["title"], "Длинная — коротко")
        self.assertTrue(summary["artifacts"]["text"].startswith("Выжимка."))
        self.assertGreater(len(summary["artifacts"]["text"].split("\n\n")), 2)
        self.assertTrue(any("Выжимка готова" in line for line in done["log"]))


if __name__ == "__main__":
    unittest.main()
