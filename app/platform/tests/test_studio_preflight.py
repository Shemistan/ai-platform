"""Оценка времени выпуска и проверка до запуска; публикация проверяет все каналы до отправки."""

import unittest

from aiplatform.studio import estimate
from tests.fixtures import ARTICLE, NEEDS_FFMPEG
from tests.helpers import StudioCase

PENDING = {s: "pending" for s in ("text", "poster_prompt", "poster", "plan", "images", "clips", "voice", "render")}
SETTINGS = {
    "poster_mode": "local",
    "draw_images": True,
    "images_mode": "local",
    "image_model": "z",
    "upscale_images": "none",
    "voice_mode": "tts",
    "engine": "higgs",
    "tempo": 1.0,
}


class RatesTest(unittest.TestCase):
    def test_medians_from_history(self):
        photo = [
            {"status": "done", "mode": "txt2img", "model": "z", "started_at": 0, "finished_at": 40},
            {"status": "done", "mode": "txt2img", "model": "z", "started_at": 0, "finished_at": 60},
            {"status": "done", "mode": "upscale", "width": 4096, "height": 2340, "started_at": 0, "finished_at": 100},
            {"status": "failed", "mode": "txt2img", "model": "z", "started_at": 0, "finished_at": 999},
        ]
        audio = [{"status": "done", "engine": "higgs", "chars": 1000, "started_at": 0, "finished_at": 50}]
        render = {"status": "done", "started_at": 10, "updated_at": 110, "media_s": 200}
        rates = estimate.rates(photo, [], audio, [{"steps": {"render": render}}])
        self.assertEqual(rates.image_s("z"), 50)
        self.assertEqual(rates.image_s("other"), 50)  # нет истории модели — медиана по остальным
        self.assertEqual(rates.upscale_s("4k"), 100)
        self.assertEqual(rates.upscale_s("2k"), estimate.DEFAULTS["upscale-2k"])
        self.assertAlmostEqual(rates.tts_s("higgs", 2000), 100)
        self.assertEqual(rates.render_s(100), 50)
        self.assertEqual(rates.from_history, 5)

    def test_only_unfinished_steps_count(self):
        text = " ".join(["слово"] * 260)  # ~2 мин озвучки
        full = estimate.estimate(
            scenario="article", settings=SETTINGS, text=text, plan=None, statuses=PENDING, have_images=0,
            rates=estimate.Rates(),
        )  # fmt: skip
        steps = [p["step"] for p in full["parts"]]
        self.assertEqual(steps, ["poster_prompt", "poster", "plan", "images", "voice", "render"])
        self.assertEqual(full["video_s"], 120)
        half = estimate.estimate(
            scenario="article", settings=SETTINGS | {"tempo": 2.0}, text=text, plan=None,
            statuses=PENDING | {s: "done" for s in ("poster_prompt", "poster", "plan", "images", "voice")},
            have_images=0, rates=estimate.Rates(),
        )  # fmt: skip
        self.assertEqual([p["step"] for p in half["parts"]], ["render"])
        self.assertEqual(half["video_s"], 60)
        self.assertLess(half["total_s"], full["total_s"])


@NEEDS_FFMPEG
class PreflightTest(StudioCase):
    def check(self, **payload):
        status, body = self.json("POST", "/api/studio/preflight", payload)
        self.assertEqual(status, 200, body)
        return body

    def test_draft_form_warnings(self):
        body = self.check(scenario="article", text=ARTICLE, settings={"channels": ["download-9x16"]})
        texts = [x["text"] for x in body["preflight"] if x["level"] == "warn"]
        self.assertTrue(any("шортс 9:16" in t for t in texts), body)
        self.assertGreater(body["estimate"]["total_s"], 0)
        fine = self.check(scenario="article", text=ARTICLE, settings={"channels": ["download-9x16"], "shorts": True})
        # Возраст реестра зависит от сегодняшней даты — его проверяет test_compliance.RegistryFreshnessTest.
        self.assertEqual([x for x in fine["preflight"] if "Реестр" not in x["text"]], [])

        long = "\n\n".join(" ".join(["слово"] * 100) for _ in range(20))  # 2000 слов ≈ 15 мин
        body = self.check(scenario="article", text=long, settings={"poster_mode": "upload"})
        warns = [x["text"] for x in body["preflight"] if x["level"] == "warn"]
        infos = [x["text"] for x in body["preflight"] if x["level"] == "info"]
        self.assertTrue(any("серии" in t for t in warns), warns)
        self.assertTrue(any("постер" in t for t in infos), infos)
        status, _ = self.json("POST", "/api/studio/preflight", {"scenario": "nope"})
        self.assertEqual(status, 400)

    def test_project_view_and_publish_checks_all_channels_first(self):
        settings = {"channels": ["download-16x9", "download-9x16"]}
        project = self.create(scenario="article", text=ARTICLE, settings=settings)
        self.assertGreater(project["estimate"]["total_s"], 0)
        self.assertTrue(any(x["level"] == "warn" for x in project["preflight"]))

        self.json("POST", f"/api/studio/projects/{project['id']}/run", {})
        self.settle(project["id"])
        self.json("POST", f"/api/studio/projects/{project['id']}/approve", {"step": "publish"})
        done = self.settle(project["id"])
        publish = next(s for s in done["steps"] if s["id"] == "publish")
        self.assertEqual(publish["status"], "failed", done["log"])
        self.assertIn("Ничего не отправлено", publish["error"])
        self.assertIn("Шортс", publish["error"])
        self.assertFalse((self.folder(project["id"]) / "publish").exists())  # 16:9 тоже не отправлен
        self.assertEqual(done["estimate"]["parts"], [])  # всё, что считается, уже готово
        render = next(s for s in done["steps"] if s["id"] == "render")
        self.assertGreater(render["media_s"], 0)
        self.assertTrue(render["started_at"])


if __name__ == "__main__":
    unittest.main()
