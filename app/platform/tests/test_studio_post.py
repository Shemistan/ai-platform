"""Сценарий «Пост в канал»: свой постер, свой mp3 и подпись → видео или картинка с аудио."""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.core import media
from aiplatform.fakes import fake_narrate
from aiplatform.studio import publish
from tests.test_studio_flow import NEEDS_FFMPEG, StudioCase, png_bytes

TOKEN = "123456:" + "A" * 35
CAPTION = "**Новая статья**\n\nО том, как устроены очереди — [читать](https://example.dev/q)"


@NEEDS_FFMPEG
class PostFlowTest(StudioCase):
    def setUp(self):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, folder)
        channels = [
            {"id": "tg", "label": "TG", "type": "telegram", "chat_id": "-1001234567890", "token_env": "AIP_TG_TEST"},
            {"id": "file", "label": "Файл", "type": "manual", "format": "landscape"},
        ]
        (folder / "channels.json").write_text(json.dumps(channels), encoding="utf-8")
        self.extra_env = StudioCase.extra_env | {"AIP_CHANNELS": str(folder / "channels.json")}
        super().setUp()

    def mp3(self, seconds=4.0) -> bytes:
        path = Path(self.tmp.name) / "v.mp3"
        fake_narrate.write_mp3(path, seconds)
        return path.read_bytes()

    def test_post_from_own_poster_and_mp3(self):
        status, cfg = self.json("GET", "/api/studio/config")
        self.assertEqual(cfg["scenario_defaults"]["post"]["voice_mode"], "upload")
        project = self.create(scenario="post", post=CAPTION, run=True)
        pid = project["id"]
        self.assertEqual(project["title"], "Новая статья")
        self.assertEqual(project["settings"]["poster_mode"], "upload")
        waiting = self.settle(pid)
        steps = self.statuses(waiting)
        self.assertEqual((steps["poster"], steps["voice"]), ("waiting", "waiting"), waiting["log"])
        for skipped in ("poster_prompt", "plan", "images", "clips"):
            self.assertEqual(steps[skipped], "skipped", skipped)
        self.assertIn("подпись:", "\n".join(waiting["log"]))

        self.assertEqual(self.call("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())[0], 200)
        self.assertEqual(self.call("POST", f"/api/studio/projects/{pid}/upload/voice", self.mp3())[0], 200)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertEqual(self.statuses(done)["short"], "skipped")
        video = self.folder(pid) / "video.mp4"
        self.assertAlmostEqual(media.duration(video), 4.0, delta=0.6)
        self.assertTrue((self.folder(pid) / "video-cover.jpg").is_file(), "с оформлением — обложка для Telegram")

        status, view = self.json("POST", f"/api/studio/projects/{pid}/publish", {"channels": ["file"]})
        self.assertEqual(status, 202, view)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        description = (self.folder(pid) / "publish" / "file" / "description.txt").read_text(encoding="utf-8")
        self.assertIn("читать (https://example.dev/q)", description)

    def test_photo_and_audio_in_two_posts(self):
        settings = {"post_format": "photo", "branding": False}
        project = self.create(scenario="post", post=CAPTION, settings=settings)
        pid = project["id"]
        self.call("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())
        self.call("POST", f"/api/studio/projects/{pid}/upload/voice", self.mp3(2.0))
        calls = []

        def fake_post(url, body, ctype):
            calls.append((url.rsplit("/", 1)[-1], body))
            return {"ok": True, "result": {"message_id": len(calls)}}

        with mock.patch.dict(os.environ, {"AIP_TG_TEST": TOKEN}), mock.patch.object(publish, "_http_post", fake_post):
            self.json("POST", f"/api/studio/projects/{pid}/run", {})
            self.settle(pid)
            self.json("POST", f"/api/studio/projects/{pid}/publish", {"channels": ["tg"]})
            done = self.settle(pid)
        self.assertEqual(self.statuses(done)["publish"], "done", done["log"])
        # Последним — число подписчиков: от него считается прирост в «Что работает».
        self.assertEqual([c[0] for c in calls], ["sendPhoto", "sendAudio", "getChatMemberCount"])
        self.assertIn(b"<b>", calls[0][1])
        self.assertIn(b'name="duration"', calls[1][1])
        self.assertIn("двумя постами", done["published"][0]["message"])

    def test_validation(self):
        status, body = self.json("POST", "/api/studio/projects", {"scenario": "post", "post": "  "})
        self.assertEqual(status, 400, body)
        status, body = self.json(
            "POST", "/api/studio/projects", {"scenario": "post", "post": "т", "settings": {"post_format": "gif"}}
        )
        self.assertEqual(status, 400, body)


if __name__ == "__main__":
    unittest.main()
