"""Готовый выпуск: параметры шага меняются в его карточке, «Перезапустить шаг» пересобирает шаг уже с ними."""

import unittest

from aiplatform.core import media
from tests.fixtures import ARTICLE, NEEDS_FFMPEG
from tests.helpers import StudioCase


@NEEDS_FFMPEG
class RerunWithSettingsTest(StudioCase):
    def test_voice_and_frame_change_on_ready_project(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"engine": "silero"}, run=True)
        pid = project["id"]
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertEqual(media.video_size(self.folder(pid) / "video.mp4"), (480, 270))

        # Шаг «Озвучка»: другой движок и голос — озвучивает заново ими, видео ждёт пересборки.
        status, rerun = self.json(
            "POST", f"/api/studio/projects/{pid}/run",
            {"step": "voice", "settings": {"engine": "supertonic", "voice": "F2", "voice_mode": "tts"}},
        )  # fmt: skip
        self.assertEqual(status, 202, rerun)
        self.assertEqual((rerun["settings"]["engine"], rerun["settings"]["voice"]), ("supertonic", "F2"))
        voiced = self.settle(pid)
        steps = self.statuses(voiced)
        self.assertEqual((steps["voice"], steps["render"]), ("done", "pending"), voiced["log"])
        _, audios = self.json("GET", "/api/audio/jobs")
        self.assertEqual((audios["jobs"][0]["engine"], audios["jobs"][0]["voice"]), ("supertonic", "F2"))

        # Шаг «Сборка видео»: квадрат вместо 16:9.
        status, _ = self.json(
            "POST", f"/api/studio/projects/{pid}/run", {"step": "render", "settings": {"frame": "square"}}
        )
        self.assertEqual(status, 202)
        rendered = self.settle(pid)
        self.assertEqual(self.statuses(rendered)["render"], "done", rendered["log"])
        self.assertEqual(rendered["settings"]["frame"], "square")
        self.assertEqual(self.statuses(rendered)["images"], "done", "картинки слайдов из-за кадра не перерисовываются")
        self.assertEqual(media.video_size(self.folder(pid) / "video.mp4"), (270, 270))

    def test_bad_step_settings_change_nothing(self):
        project = self.create(scenario="article", text=ARTICLE)
        pid = project["id"]
        status, body = self.json(
            "POST", f"/api/studio/projects/{pid}/run", {"step": "render", "settings": {"frame": "круг"}}
        )
        self.assertEqual(status, 400, body)
        status, body = self.json("POST", f"/api/studio/projects/{pid}/run", {"settings": {"frame": "square"}})
        self.assertEqual(status, 400, body)
        _, same = self.json("GET", f"/api/studio/projects/{pid}")
        self.assertFalse(same["running"])
        self.assertEqual(same["settings"]["frame"], "landscape")


if __name__ == "__main__":
    unittest.main()
