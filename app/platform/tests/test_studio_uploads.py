"""Картинки и видео слайдов из внешней модели: промпты на странице, загрузка в окна, сборка с ними."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiplatform.core import media, uploads
from aiplatform.studio import slides, steps
from tests.test_studio_flow import ARTICLE, NEEDS_FFMPEG, StudioCase, png_bytes


def clip_bytes(seconds: float = 1.0) -> bytes:
    """Короткий mp4 без звука — как ролик из внешнего генератора видео."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "c.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=s=160x90:d={seconds}:r=25"]
            + ["-pix_fmt", "yuv420p", str(path)],
            check=True,
        )
        return path.read_bytes()


class SniffVideoTest(unittest.TestCase):
    def test_signatures(self):
        self.assertEqual(uploads.sniff_video(b"\0\0\0\x20ftypisom" + b"\0" * 20), "mp4")
        self.assertEqual(uploads.sniff_video(b"\0\0\0\x14ftypqt  " + b"\0" * 20), "mov")
        self.assertEqual(uploads.sniff_video(b"\x1a\x45\xdf\xa3" + b"\0" * 20), "webm")
        self.assertIsNone(uploads.sniff_video(b"\0\0\0\x20ftypM4A " + b"\0" * 20), "аудио M4A — не видео")
        self.assertIsNone(uploads.sniff_video(png_bytes()))


class SlideMediaTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_prompt_change_invalidates_upload(self):
        folder = self.dir / steps.IMAGES
        steps.save_slide_media(folder, 1, "jpg", b"jpeg", "a fox", steps.IMAGES)
        self.assertEqual(steps.slide_media(folder, 1, steps.IMAGES, "a fox"), folder / "01.jpg")
        self.assertIsNone(steps.slide_media(folder, 1, steps.IMAGES, "a wolf"))
        steps.save_slide_media(folder, 1, "png", b"png", "a wolf", steps.IMAGES)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["01.json", "01.png"], "прежний формат удалён")

    def test_clip_prompt_falls_back_to_image_prompt(self):
        self.assertEqual(steps.clip_prompt(slides.Slide(0, "t", video_prompt="pan left")), "pan left")
        self.assertIn("a fox", steps.clip_prompt(slides.Slide(0, "t", image_prompt="a fox")))
        self.assertEqual(steps.clip_prompt(slides.Slide(0, "t")), "")

    def test_scenes_prefer_clip_then_image(self):
        plan = [slides.Slide(0, "a"), slides.Slide(1, "b"), slides.Slide(2, "c")]
        for name in ("images/01.png", "images/02.jpg", "clips/01.mp4"):
            (self.dir / name).parent.mkdir(exist_ok=True)
            (self.dir / name).write_bytes(b"x")
        got = steps.scenes(self.dir, plan, [1.0, 1.0, 1.0], clips=True)
        self.assertEqual([(s["clip"] and s["clip"].name, s["image"] and s["image"].name) for s in got],
                         [("01.mp4", "01.png"), (None, "02.jpg"), (None, None)])  # fmt: skip
        off = steps.scenes(self.dir, plan, [1.0, 1.0, 1.0], images=False, clips=False)
        self.assertTrue(all(s["clip"] is None and s["image"] is None for s in off), "выключено — градиент")


@NEEDS_FFMPEG
class ExternalImagesTest(StudioCase):
    def test_images_wait_for_uploads_then_render(self):
        settings = {"images_mode": "upload", "poster_mode": "upload"}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        pid = project["id"]
        # Постер ждёт загрузки — снимаем его, чтобы дойти до картинок.
        self.settle(pid)
        self.assertEqual(self.call("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())[0], 200)
        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        waiting = self.settle(pid)
        self.assertEqual(self.statuses(waiting)["images"], "waiting", waiting["log"])
        self.assertEqual(self.json("GET", "/api/photo/jobs")[1]["jobs"], [], "локальная модель не запускалась")

        rows = waiting["slides"]
        self.assertEqual([s["key"] for s in rows], ["01", "02", "03", "04"])
        self.assertTrue(all(s["image_prompt"] and s["video_prompt"] for s in rows))
        self.assertIsNone(rows[0]["image"])

        status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/01", png_bytes())
        self.assertEqual(status, 200, view)
        images = next(s for s in view["steps"] if s["id"] == "images")
        self.assertEqual((images["status"], images["note"]), ("waiting", "Загружено вручную: 1 из 4."))
        self.assertIsNotNone(view["slides"][0]["image"])
        self.assertEqual(self.call("GET", view["slides"][0]["image"])[0], 200)

        for key in ("02", "03", "04"):
            status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/{key}", png_bytes())
        self.assertEqual(self.statuses(view)["images"], "done", "все на месте — шаг готов сам")
        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertEqual(self.json("GET", "/api/photo/jobs")[1]["jobs"], [])

        # Замена картинки готового выпуска — пересобрать видео.
        status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/02", png_bytes())
        self.assertEqual((self.statuses(view)["images"], self.statuses(view)["render"]), ("done", "pending"))

    def test_continue_without_missing(self):
        settings = {"images_mode": "upload", "poster_mode": "upload"}
        project = self.create(scenario="article", text=ARTICLE, settings=settings)
        pid = project["id"]
        self.call("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())
        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        self.settle(pid)
        self.json("POST", f"/api/studio/projects/{pid}/upload/images/03", png_bytes())
        status, _ = self.json("POST", f"/api/studio/projects/{pid}/approve", {"step": "images"})
        self.assertEqual(status, 200)
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertTrue(any("без картинки: слайды 1, 2, 4" in line for line in done["log"]), done["log"])


@NEEDS_FFMPEG
class ExternalClipsTest(StudioCase):
    def test_clips_become_slide_backgrounds(self):
        settings = {"clips": True, "draw_images": False, "shorts": True}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        pid = project["id"]
        waiting = self.settle(pid)
        self.assertEqual(self.statuses(waiting)["clips"], "waiting", waiting["log"])
        clips_step = next(s for s in waiting["steps"] if s["id"] == "clips")
        self.assertIn("готово 0 из 4", clips_step["note"])

        clip = clip_bytes()
        for key in ("01", "02", "03", "04"):
            status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/clips/{key}", clip)
            self.assertEqual(status, 200, view)
        self.assertEqual(self.statuses(view)["clips"], "done")
        self.assertIsNotNone(view["slides"][0]["clip"])
        self.assertEqual(self.call("GET", view["slides"][0]["clip"])[0], 200)

        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        done = self.settle(pid)
        steps_now = self.statuses(done)
        self.assertEqual((steps_now["render"], steps_now["short"]), ("done", "done"), done["log"])
        folder = self.folder(pid)
        # Ролик в 1 с крутится по кругу: видео всё равно покрывает всю озвучку.
        self.assertAlmostEqual(media.duration(folder / "video.mp4"), media.duration(folder / "voice.mp3"), delta=0.6)
        self.assertEqual(media.video_size(folder / "short.mp4"), (270, 480))

    def test_bad_uploads(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"clips": True})
        pid = project["id"]
        base = f"/api/studio/projects/{pid}/upload"
        # Плана ещё нет — слайдов нет.
        self.assertEqual(self.json("POST", f"{base}/clips/01", clip_bytes())[0], 404)
        plan = {"slides": [{"para": 0, "title": "Один", "image_prompt": "a fox", "video_prompt": "a fox runs"}]}
        self.json("POST", f"/api/studio/projects/{pid}/artifacts/plan", {"text": json.dumps(plan)})
        cases = [
            (f"{base}/clips/01", png_bytes(), 400, "MP4"),
            (f"{base}/images/01", clip_bytes(), 400, "PNG"),
            (f"{base}/clips/02", clip_bytes(), 404, "нет слайда 2"),
            (f"{base}/frames/01", png_bytes(), 404, "images/NN"),
            (f"{base}/clips/1", clip_bytes(), 404, "images/NN"),
        ]
        for path, body, code, message in cases:
            with self.subTest(path):
                status, err = self.json("POST", path, body)
                self.assertEqual(status, code, err)
                self.assertIn(message, err["error"])
        status, view = self.json("POST", f"{base}/clips/01", clip_bytes())
        self.assertEqual((status, self.statuses(view)["clips"]), (200, "done"))


class OldProjectTest(StudioCase):
    def test_project_without_clips_step_loads(self):
        project = self.create(scenario="article", text="Абзац.")
        path = self.folder(project["id"]) / "project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["steps"]["clips"]
        for key in ("clips", "images_mode"):
            del data["settings"][key]
        path.write_text(json.dumps(data), encoding="utf-8")
        from aiplatform import server as server_module

        app = server_module.App(self.config)
        self.addCleanup(app.close)
        studio = app.modules["studio"]
        view = studio.view(project["id"])
        self.assertEqual(self.statuses(view)["clips"], "skipped")
        self.assertEqual((view["settings"]["clips"], view["settings"]["images_mode"]), (False, "local"))
        updated = studio.update_settings(project["id"], {"shorts": True})
        self.assertFalse(updated["settings"]["clips"])


if __name__ == "__main__":
    unittest.main()
