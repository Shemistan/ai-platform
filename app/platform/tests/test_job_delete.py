"""Удаление неудачного результата на вкладках «Фото», «Видео», «Аудио» (UX-029)."""

from pathlib import Path

from tests.helpers import PhotoServerCase, VideoServerCase
from tests.test_audio import TEXT, AudioServerCase


class PhotoDeleteTest(PhotoServerCase):
    def test_delete_done_job(self):
        done, _, _ = self.run_job({"prompt": "кот", "size": "512x512"})
        folder = Path(self.tmp.name) / "photo" / "jobs" / done["id"]
        self.assertTrue(folder.is_dir())
        status, body = self.json("POST", f"/api/photo/jobs/{done['id']}/delete")
        self.assertEqual((status, body), (200, {"deleted": done["id"]}))
        self.assertFalse(folder.exists())
        _, listing = self.json("GET", "/api/photo/jobs")
        self.assertNotIn(done["id"], [j["id"] for j in listing["jobs"]])
        self.assertEqual(self.json("POST", f"/api/photo/jobs/{done['id']}/delete")[0], 404)

    def test_active_job_is_not_deleted(self):
        with self.app.ctx.gate.hold("Видео"):
            _, job = self.json("POST", "/api/photo/jobs", {"prompt": "кот", "size": "512x512"})
            status, err = self.json("POST", f"/api/photo/jobs/{job['id']}/delete")
            self.assertEqual(status, 409)
            self.assertIn("отмени", err["error"])
            self.json("POST", f"/api/photo/jobs/{job['id']}/cancel")
            self.wait(job["id"])

    def test_source_of_running_upscale_is_not_deleted(self):
        done, _, _ = self.run_job({"prompt": "кот", "size": "768x768"})
        with self.app.ctx.gate.hold("Видео"):
            _, big = self.json("POST", f"/api/photo/jobs/{done['id']}/upscale", {"target": "2k"})
            status, err = self.json("POST", f"/api/photo/jobs/{done['id']}/delete")
            self.assertEqual(status, 409)
            self.assertIn("увеличивают", err["error"])
            self.json("POST", f"/api/photo/jobs/{big['id']}/cancel")
            self.wait(big["id"])
        self.assertEqual(self.json("POST", f"/api/photo/jobs/{done['id']}/delete")[0], 200)


class VideoDeleteTest(VideoServerCase):
    def test_delete_done_job(self):
        _, job = self.json("POST", "/api/video/jobs", {"prompt": "p"})
        self.wait(job["id"])
        status, _ = self.json("POST", f"/api/video/jobs/{job['id']}/delete")
        self.assertEqual(status, 200)
        self.assertFalse((Path(self.tmp.name) / "video" / "jobs" / job["id"]).exists())
        self.assertEqual(self.json("GET", f"/api/video/jobs/{job['id']}")[0], 404)


class AudioDeleteTest(AudioServerCase):
    def test_delete_done_job(self):
        _, job = self.json("POST", "/api/audio/jobs", {"text": TEXT, "engine": "silero", "voice": "baya"})
        self.wait(job["id"])
        status, _ = self.json("POST", f"/api/audio/jobs/{job['id']}/delete")
        self.assertEqual(status, 200)
        self.assertFalse((Path(self.tmp.name) / "audio" / "jobs" / job["id"]).exists())
        _, listing = self.json("GET", "/api/audio/jobs")
        self.assertEqual(listing["jobs"], [])
