"""Картинки и видео слайдов из внешней модели: промпты на странице, загрузка в окна, сборка с ними."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiplatform.core import media, uploads
from aiplatform.studio import slides, steps
from tests.fixtures import ARTICLE, NEEDS_FFMPEG, png_bytes
from tests.helpers import StudioCase


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

    def test_clips_only_for_chosen_slides_and_capped(self):
        plan = [slides.Slide(n, f"s{n}", image_prompt="img", video_prompt="vid" if n % 2 else "") for n in range(9)]
        clips = steps.media_prompts(steps.CLIPS, plan)
        self.assertEqual([n for n, _ in clips], [2, 4, 6], "только слайды с video_prompt, не больше MAX_CLIPS")
        self.assertEqual(len(steps.media_prompts(steps.IMAGES, plan)), 9)

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

        # Картинки нужны только слайдам-картинкам: динамические рисует сборка.
        rows = [s for s in waiting["slides"] if s["image_prompt"]]
        self.assertEqual([s["key"] for s in rows], ["01", "03", "05", "07"])
        self.assertTrue(all(s["image_prompt"] for s in rows))
        self.assertEqual(
            [bool(s["video_prompt"]) for s in rows], [False, True, True, True], "ролики — ключевые моменты"
        )
        self.assertIsNone(rows[0]["image"])

        status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/01", png_bytes())
        self.assertEqual(status, 200, view)
        images = next(s for s in view["steps"] if s["id"] == "images")
        self.assertEqual((images["status"], images["note"]), ("waiting", "Загружено вручную: 1 из 4."))
        self.assertIsNotNone(view["slides"][0]["image"])
        self.assertEqual(self.call("GET", view["slides"][0]["image"])[0], 200)

        for key in ("03", "05", "07"):
            status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/{key}", png_bytes())
        self.assertEqual(self.statuses(view)["images"], "done", "все на месте — шаг готов сам")
        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertEqual(self.json("GET", "/api/photo/jobs")[1]["jobs"], [])

        # Замена картинки готового выпуска — видео пересобирается само.
        video = self.folder(pid) / "video.mp4"
        before = video.stat().st_mtime_ns
        status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/03", png_bytes())
        self.assertEqual((status, self.statuses(view)["images"]), (200, "done"))
        rebuilt = self.settle(pid)
        self.assertEqual(self.statuses(rebuilt)["render"], "done", rebuilt["log"])
        self.assertGreater(video.stat().st_mtime_ns, before)

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
        self.assertTrue(any("без картинки: слайды 1, 5, 7" in line for line in done["log"]), done["log"])


@NEEDS_FFMPEG
class ExternalClipsTest(StudioCase):
    def test_clips_become_slide_backgrounds(self):
        settings = {"clips": True, "clips_mode": "upload", "draw_images": False, "shorts": True}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        pid = project["id"]
        waiting = self.settle(pid)
        self.assertEqual(self.statuses(waiting)["clips"], "waiting", waiting["log"])
        clips_step = next(s for s in waiting["steps"] if s["id"] == "clips")
        self.assertIn("готово 0 из 3", clips_step["note"])

        clip = clip_bytes()
        for key in ("03", "05", "07"):
            status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/clips/{key}", clip)
            self.assertEqual(status, 200, view)
        self.assertEqual(self.statuses(view)["clips"], "done")
        self.assertIsNone(view["slides"][0]["clip"], "первому слайду ролик не нужен")
        self.assertEqual(self.call("GET", view["slides"][2]["clip"])[0], 200)

        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        done = self.settle(pid)
        steps_now = self.statuses(done)
        self.assertEqual((steps_now["render"], steps_now["short"]), ("done", "done"), done["log"])
        folder = self.folder(pid)
        # Ролик в 1 с крутится по кругу: видео всё равно покрывает всю озвучку.
        self.assertAlmostEqual(media.duration(folder / "video.mp4"), media.duration(folder / "voice.mp3"), delta=0.6)
        self.assertEqual(media.video_size(folder / "short.mp4"), (270, 480))

    def test_bad_uploads(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"clips": True, "clips_mode": "upload"})
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


@NEEDS_FFMPEG
class UploadWhileRunningTest(StudioCase):
    process_env = StudioCase.process_env | {"FAKE_TTS_STEP_DELAY": "0.4"}

    def step(self, project, step_id):
        return next(s for s in project["steps"] if s["id"] == step_id)

    def test_uploads_while_voice_runs_then_render_starts_itself(self):
        settings = {"images_mode": "upload"}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        pid = project["id"]
        # Картинки ждут человека, а озвучка тем временем идёт.
        running = self.poll(
            f"/api/studio/projects/{pid}",
            lambda p: self.statuses(p)["images"] == "waiting" and self.statuses(p)["voice"] == "running",
        )
        self.assertTrue(running["running"])
        self.assertFalse(self.step(running, "images")["locked"])
        self.assertTrue(self.step(running, "voice")["locked"])

        # Озвучку, которая выполняется прямо сейчас, подменить нельзя; картинки — можно.
        status, err = self.json("POST", f"/api/studio/projects/{pid}/upload/voice", b"ID3" + b"\0" * 64)
        self.assertEqual(status, 409, err)
        self.assertIn("Озвучка", err["error"])
        for key in ("01", "03", "05", "07"):
            status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/{key}", png_bytes())
            self.assertEqual(status, 200, view)

        # Никаких кнопок: озвучка доделалась, видео собралось само.
        done = self.settle(pid)
        steps_now = self.statuses(done)
        self.assertEqual((steps_now["images"], steps_now["voice"], steps_now["render"]), ("done", "done", "done"))
        self.assertEqual(steps_now["publish"], "waiting", done["log"])

    def test_stopped_project_does_not_resume_on_upload(self):
        settings = {"poster_mode": "upload", "draw_images": False}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        pid = project["id"]
        self.poll(f"/api/studio/projects/{pid}", lambda p: self.statuses(p)["voice"] == "running")
        self.json("POST", f"/api/studio/projects/{pid}/cancel", {})
        stopped = self.settle(pid)
        self.assertEqual(self.statuses(stopped)["voice"], "pending")
        status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/poster", png_bytes())
        self.assertEqual(status, 200, view)
        self.assertFalse(view["running"], "остановленный кнопкой выпуск сам не запускается")
        self.assertEqual(self.statuses(view)["voice"], "pending")


@NEEDS_FFMPEG
class LocalClipsTest(StudioCase):
    process_env = StudioCase.process_env | {"FAKE_LTX_STEP_DELAY": "0.01"}

    def test_clips_generated_on_video_tab(self):
        settings = {"clips": True, "draw_images": False}
        project = self.create(scenario="article", text=ARTICLE, settings=settings, run=True)
        self.assertEqual(project["settings"]["clips_mode"], "local", "по умолчанию — локальная модель")
        done = self.settle(project["id"], timeout=180)
        steps_now = self.statuses(done)
        self.assertEqual((steps_now["clips"], steps_now["render"]), ("done", "done"), done["log"])
        _, videos = self.json("GET", "/api/video/jobs")
        self.assertEqual(len(videos["jobs"]), 3, "ролики — только в ключевых моментах, не на каждый слайд")
        job = videos["jobs"][0]
        self.assertEqual((job["duration_s"], job["width"], job["height"], job["frames"]), (5, 704, 448, []))
        self.assertEqual([bool(s["clip"]) for s in done["slides"]], [False, False, True, False, True, False, True])

        # Повторный запуск шага не перегенерирует готовые ролики.
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {"step": "clips"})
        self.settle(project["id"])
        self.assertEqual(len(self.json("GET", "/api/video/jobs")[1]["jobs"]), 3)

    def test_bad_video_model(self):
        status, body = self.json(
            "POST", "/api/studio/projects", {"scenario": "article", "text": "т", "settings": {"video_model": "nope"}}
        )
        self.assertEqual(status, 400, body)
        self.assertIn("модель видео", body["error"])
        _, cfg = self.json("GET", "/api/studio/config")
        self.assertIn("ltx-2.3-q4", [m["id"] for m in cfg["video_models"]])


@NEEDS_FFMPEG
class UpscaleImagesTest(StudioCase):
    def photo_jobs(self):
        jobs = self.json("GET", "/api/photo/jobs")[1]["jobs"]
        return [j for j in jobs if j["mode"] != "upscale"], [j for j in jobs if j["mode"] == "upscale"]

    def test_generated_images_upscaled(self):
        project = self.create(scenario="article", text=ARTICLE, settings={"upscale_images": "2k"}, run=True)
        pid = project["id"]
        done = self.settle(pid)
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        made, upscaled = self.photo_jobs()
        self.assertEqual((len(made), len(upscaled)), (5, 5), "постер и 4 картинки — каждая увеличена")
        folder = self.folder(pid)
        for path in [folder / "poster.png", *sorted((folder / "images").glob("*.png"))]:
            self.assertEqual(uploads.png_size(path.read_bytes()), (2048, 1170), path.name)

        # Сменили только увеличение — картинки не перерисовываются, а увеличиваются из тех же задач.
        status, view = self.json("POST", f"/api/studio/projects/{pid}/settings", {"upscale_images": "4k"})
        self.assertEqual(status, 200, view)
        self.json("POST", f"/api/studio/projects/{pid}/run", {})
        again = self.settle(pid)
        self.assertEqual(self.statuses(again)["render"], "done", again["log"])
        made, upscaled = self.photo_jobs()
        self.assertEqual((len(made), len(upscaled)), (5, 10), "ничего не перерисовано — только увеличено заново")
        for path in [folder / "poster.png", folder / "images" / "01.png"]:
            self.assertEqual(uploads.png_size(path.read_bytes()), (4096, 2340), path.name)

    def test_bad_upscale_setting(self):
        payload = {"scenario": "article", "text": "т", "settings": {"upscale_images": "8k"}}
        status, body = self.json("POST", "/api/studio/projects", payload)
        self.assertEqual(status, 400, body)
        self.assertIn("Некорректный запрос", body["error"])


class ManySlidesUploadTest(StudioCase):
    def test_upload_and_serve_slide_101(self):
        text = "\n\n".join(f"Абзац {n}." for n in range(1, 102))
        project = self.create(scenario="article", text=text, settings={"images_mode": "upload"})
        pid = project["id"]
        plan = {"slides": [{"para": n, "title": f"Слайд {n + 1}", "image_prompt": "a fox"} for n in range(101)]}
        status, _ = self.json("POST", f"/api/studio/projects/{pid}/artifacts/plan", {"text": json.dumps(plan)})
        self.assertEqual(status, 200)
        status, view = self.json("POST", f"/api/studio/projects/{pid}/upload/images/101", png_bytes())
        self.assertEqual(status, 200, view)
        row = view["slides"][100]
        self.assertEqual(row["key"], "101")
        self.assertIsNotNone(row["image"])
        self.assertEqual(self.call("GET", row["image"])[0], 200, "картинка слайда ≥100 отдаётся")


class OldProjectTest(StudioCase):
    def test_project_without_clips_step_loads(self):
        project = self.create(scenario="article", text="Абзац.")
        path = self.folder(project["id"]) / "project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["steps"]["clips"]
        for key in ("clips", "images_mode", "clips_mode", "video_model", "upscale_images"):
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

    def test_migration_on_load_keeps_updated_at(self):
        # BE-014: миграция при старте — не движение выпуска, срок уборки и время на доске прежние.
        project = self.create(scenario="article", text="Абзац.")
        path = self.folder(project["id"]) / "project.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["steps"]["clips"]
        data["updated_at"] = 1_000_000.0
        path.write_text(json.dumps(data), encoding="utf-8")
        from aiplatform import server as server_module

        app = server_module.App(self.config)
        self.addCleanup(app.close)
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["updated_at"], 1_000_000.0)


if __name__ == "__main__":
    unittest.main()
