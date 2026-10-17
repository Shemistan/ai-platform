"""Каналы по умолчанию для каждого сценария (PRD-005)."""

from tests.fixtures import ARTICLE
from tests.helpers import JournalCase


class DefaultChannelsTest(JournalCase):
    def set_defaults(self, **chosen):
        status, body = self.json("POST", "/api/settings/section/default_channels", chosen)
        self.assertEqual(status, 200, body)
        return body

    def board_item(self, pid):
        return next(p for p in self.json("GET", "/api/studio/board")[1]["projects"] if p["id"] == pid)

    def test_new_article_gets_article_defaults(self):
        body = self.set_defaults(article=["tg-b"])
        self.assertEqual(next(s for s in body["scenarios"] if s["id"] == "article")["channels"], ["tg-b"])
        project = self.create(scenario="article", text=ARTICLE)
        self.assertEqual(project["settings"]["channels"], ["tg-b"])
        post = self.json(
            "POST", "/api/studio/projects", {"scenario": "post", "post": "Подпись", "settings": {"channels": []}}
        )[1]
        self.assertEqual(post["settings"]["channels"], [], "явный выбор формы сильнее умолчаний")

    def test_old_release_without_channels_offers_defaults_on_board(self):
        pid = self.create(scenario="article", text=ARTICLE, settings={"channels": []})["id"]
        self.assertEqual(self.board_item(pid)["default_channels"], [])
        self.set_defaults(article=["tg-a", "tg-b"])
        item = self.board_item(pid)
        self.assertEqual(item["channels"], [])
        self.assertEqual([c["id"] for c in item["default_channels"]], ["tg-a", "tg-b"])
        self.assertEqual([c["label"] for c in item["default_channels"]], ["TG-A", "TG-B"])
        view = self.json("GET", f"/api/studio/projects/{pid}")[1]
        self.assertEqual(view["default_channels"], ["tg-a", "tg-b"])

        # Кнопка доски шлёт эти каналы в обычный publish: выпуск уходит в оба.
        (self.folder(pid) / "video.mp4").write_bytes(b"x")
        done = self.press(pid, channels=[c["id"] for c in item["default_channels"]])
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertEqual(self.tg.count("tg-a", "sendVideo"), 1)

    def test_unknown_or_missing_defaults_keep_old_behaviour(self):
        self.set_defaults(article=["нет-такого"])
        project = self.create(scenario="article", text=ARTICLE)
        self.assertEqual(project["settings"]["channels"], [])
        self.assertEqual(self.board_item(project["id"])["default_channels"], [])

    def test_bad_value_rejected(self):
        status, _ = self.json("POST", "/api/settings/section/default_channels", {"article": "tg-a"})
        self.assertEqual(status, 400)
