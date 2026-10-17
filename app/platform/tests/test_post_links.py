"""Ссылки поста перед публикацией (SEC-013): домены видны, автопубликация не пропускает чужие."""

import unittest

from aiplatform.studio import publish
from tests.test_studio_flow import ARTICLE, SOURCE, StudioCase


class DomainsTest(unittest.TestCase):
    def test_link_domains(self):
        post = "**Тема** — [релиз](https://www.Go.dev/doc/go1.26) и https://evil.example/x, ещё [тут](http://a.b.c/)."
        self.assertEqual(publish.link_domains(post), ["a.b.c", "evil.example", "go.dev"])
        self.assertEqual(publish.link_domains("без ссылок"), [])

    def test_foreign_domains(self):
        sources = ["Материал: https://go.dev/blog/x", "https://github.com/golang/go/issues/1"]
        post = "[a](https://go.dev/doc) [b](https://pkg.go.dev/x) [c](https://github.com/y) [d](https://g1thub.com/y)"
        # поддомен известного домена — свой, похожий по написанию — чужой
        self.assertEqual(publish.foreign_domains(post, sources), ["g1thub.com"])
        self.assertEqual(publish.foreign_domains(post, []), ["g1thub.com", "github.com", "go.dev", "pkg.go.dev"])


class AutoPublishLinksTest(StudioCase):
    def test_foreign_link_stops_auto_publish(self):
        settings = {"channels": ["download-16x9"], "auto_publish": True}
        project = self.create(scenario="article", text=ARTICLE, settings=settings)
        pid = project["id"]
        folder = self.folder(pid)
        (folder / "video.mp4").write_bytes(b"x")
        (folder / "post.md").write_text(
            f"Разбор сборщика мусора — [статья]({SOURCE['source_url']}), [подробнее](https://phish.example/login)\n",
            encoding="utf-8",
        )
        _, view = self.json("GET", f"/api/studio/projects/{pid}")
        self.assertEqual(view["links"], {"domains": ["example.dev", "phish.example"], "foreign": ["phish.example"]})

        self.json("POST", f"/api/studio/projects/{pid}/run", {"step": "publish"})
        done = self.settle(pid)
        step = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(step["status"], "waiting", done["log"])
        self.assertIn("phish.example", step["note"])
        self.assertIn("нет в исходном материале", step["note"])
        self.assertFalse((folder / "publish").exists())

        # Человек посмотрел и нажал «Опубликовать» — уходит.
        status, _ = self.json("POST", f"/api/studio/projects/{pid}/publish", {"channels": ["download-16x9"]})
        self.assertEqual(status, 200)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        self.assertTrue(any("ссылки в посте ведут на: example.dev, phish.example" in line for line in done["log"]))


if __name__ == "__main__":
    unittest.main()
