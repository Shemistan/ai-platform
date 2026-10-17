"""Оформление выпуска как у narrator: обложка дайджеста из его промпта, плей и плашка «подпишись»."""

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from aiplatform.core import media
from aiplatform.fakes import fake_news
from aiplatform.studio import publish, render
from aiplatform.studio.config import ASSETS
from tests.test_studio_flow import FAST, NEEDS_FFMPEG, StudioCase, png_bytes

BRANDING = render.Branding(sticker=ASSETS / "subscribe.png", play=ASSETS / "play.png")


class StickerTest(unittest.TestCase):
    def test_assets_are_png_with_size(self):
        self.assertEqual(render.png_size(BRANDING.sticker), (1200, 400))
        self.assertAlmostEqual(BRANDING.ratio, 3.0)
        with tempfile.NamedTemporaryFile(suffix=".png") as bad:
            bad.write(b"not a png at all, honestly....")
            bad.flush()
            with self.assertRaisesRegex(Exception, "не PNG"):
                render.png_size(Path(bad.name))

    def test_boxes_bottom_then_corner(self):
        lay = render.layout(render.LANDSCAPE)
        bottom, corner = render.sticker_boxes(lay, 3.0)
        self.assertEqual(bottom.width, 908)  # не выше 28 % кадра: 1080 × 0,28 × 3
        self.assertEqual((bottom.x, bottom.y), ((1920 - 908) // 2, 1080 - 303 - 32))
        self.assertEqual((corner.width, corner.x, corner.y), (460, 1920 - 460 - 32, 32))
        bottom, corner = render.sticker_boxes(render.layout(render.VERTICAL), 3.0)
        self.assertEqual((bottom.width, corner.width), (994, 540))

    def test_frame_follows_poster(self):
        self.assertEqual(render.frame(1024, 1024), "1080x1080")
        self.assertEqual(render.frame(1024, 1536), "1080x1620")
        self.assertEqual(render.frame(1536, 1024), "1080x720")
        self.assertEqual(render.frame(100, 1000), "1080x1920", "не уже 9:16")
        self.assertEqual(render.frame(1000, 100), "1080x608", "не шире 16:9")
        self.assertEqual(render.frame(0, 0), "1080x1080")
        lay = render.layout("1080x1080")
        self.assertEqual((lay.width, lay.height, lay.margin), (1080, 1080, 65))
        self.assertGreater(lay.title_chars, 20)
        self.assertEqual(render.layout("1080x1080", 0.25).width, 270)
        bottom, corner = render.sticker_boxes(lay, 3.0)
        self.assertEqual(corner.width, 346, "в квадрате плашка в углу — треть ширины, как у narrator")
        with self.assertRaisesRegex(Exception, "Кадр"):
            render.layout("квадрат")

    def test_outro_starts_before_end(self):
        self.assertEqual(render.outro_start(10.0), 10.0 - render.OUTRO_S)
        self.assertEqual(render.outro_start(1.0), 0.0, "короткий последний слайд — плашка едет сразу")

    def test_ease_is_smoothstep_in_time(self):
        p = "min(1,max(0,(t-0.800)/1.000))"
        self.assertEqual(render._ease(0.8, 1.0), f"({p})*({p})*(3-2*({p}))")


def frame(video: Path, at: float) -> bytes:
    """Кадр видео на секунде at в оттенках серого, сырыми байтами."""
    argv = ["ffmpeg", "-v", "error", "-ss", f"{at}", "-i", str(video), "-frames:v", "1"]
    argv += ["-f", "rawvideo", "-pix_fmt", "gray", "-"]
    return subprocess.run(argv, capture_output=True, check=True).stdout


@NEEDS_FFMPEG
class RenderBrandingTest(unittest.TestCase):
    def test_poster_and_slide_with_branding(self):
        renderer = render.Renderer("ffmpeg", "", scale=0.25)
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            square = folder / "cover.png"
            square.write_bytes(png_bytes())
            cancel = threading.Event()
            poster = renderer.poster_clip(square, folder / "p.mp4", 3.0, render.LANDSCAPE, cancel, BRANDING)
            self.assertEqual(media.video_size(poster), (480, 270))
            self.assertAlmostEqual(media.duration(poster), 3.0, delta=0.1)
            slide = renderer.slide_clip(
                title="Т", bullets=[], image=None, out=folder / "s.mp4", seconds=1.0,
                orientation=render.VERTICAL, cancel=cancel, branding=BRANDING,
            )  # fmt: skip
            self.assertEqual(media.video_size(slide), (270, 480))
            # Последний слайд: в конце притемнение и крупная плашка внизу — последний кадр другой.
            kwargs = {"title": "Т", "bullets": [], "image": None, "seconds": 3.0, "orientation": render.LANDSCAPE}
            kwargs |= {"cancel": cancel, "branding": BRANDING}
            plain = renderer.slide_clip(out=folder / "plain.mp4", **kwargs)
            outro = renderer.slide_clip(out=folder / "outro.mp4", outro=True, **kwargs)
            self.assertAlmostEqual(media.duration(outro), 3.0, delta=0.1)
            self.assertEqual(frame(outro, 0.2), frame(plain, 0.2), "до концовки слайд как обычно")
            self.assertNotEqual(frame(outro, 2.9), frame(plain, 2.9))
            renderer.cover_frames(poster, folder / "c.jpg", folder / "t.jpg", cancel)
            self.assertLessEqual(max(media.video_size(folder / "t.jpg")), 320)


class TelegramCoverTest(unittest.TestCase):
    def test_cover_and_thumbnail_are_attached(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            video, cover, thumb = folder / "v.mp4", folder / "v-cover.jpg", folder / "v-thumb.jpg"
            for path in (video, cover, thumb):
                path.write_bytes(b"data")
            channel = publish.Channel("tg", "TG", publish.TELEGRAM, chat_id="@x", token_env="AIP_TG_TEST")
            sent = {}

            def fake_post(url, body, ctype):
                sent["body"] = body
                return {"ok": True, "result": {"message_id": 1}}

            with mock.patch.dict(os.environ, {"AIP_TG_TEST": "123456:" + "S" * 35}):
                publish.publish(
                    channel, video=video, title="Т", post="П", out_dir=folder, http_post=fake_post,
                    cover=cover, thumb=thumb,
                )  # fmt: skip
        body = sent["body"]
        self.assertIn(b'name="cover"\r\n\r\nattach://cover', body)
        self.assertIn(b'name="cover"; filename="v-cover.jpg"\r\nContent-Type: image/jpeg', body)
        self.assertIn(b'name="thumbnail"; filename="v-thumb.jpg"', body)
        self.assertIn(b'name="video"; filename="v.mp4"\r\nContent-Type: video/mp4', body)


@NEEDS_FFMPEG
class DigestBrandingTest(StudioCase):
    def test_digest_uses_narrator_prompt_and_branding(self):
        project = self.create(scenario="digest", day="2026-10-01", settings={"poster_mode": "local"}, run=True)
        self.assertTrue(project["settings"]["branding"], "у дайджеста оформление по умолчанию")
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertEqual(done["artifacts"]["poster_prompt"].strip(), fake_news.IMAGE_PROMPT.strip())
        self.assertIn("промпт обложки — из выпуска narrator", "\n".join(done["log"]))
        _, photos = self.json("GET", "/api/photo/jobs")
        poster_job = photos["jobs"][-1]  # история — новые сверху: постер ставится первым
        self.assertEqual((poster_job["width"], poster_job["height"]), (1024, 1024))
        folder = self.folder(project["id"])
        self.assertTrue((folder / "video-cover.jpg").is_file())
        self.assertTrue((folder / "video-thumb.jpg").is_file())
        # Кадр — как постер (квадрат), картинки слайдов — тоже квадратные.
        self.assertEqual(done["settings"]["frame"], "poster")
        self.assertEqual(media.video_size(folder / "video.mp4"), (270, 270))  # 1080×1080 при AIP_RENDER_SCALE=0.25
        self.assertTrue(all((j["width"], j["height"]) == (1024, 1024) for j in photos["jobs"]), photos["jobs"])
        self.assertIn("кадр 1080×1080", "\n".join(done["log"]))
        # Публикуют на ×1,25, как в narrator: видео короче озвучки во столько же раз.
        self.assertEqual(done["settings"]["tempo"], 1.25)
        voice_s = media.duration(folder / "voice.mp3")
        self.assertAlmostEqual(media.duration(folder / "voice-x1.25.mp3"), voice_s / 1.25, delta=0.3)
        self.assertAlmostEqual(media.duration(folder / "video.mp4"), voice_s / 1.25, delta=0.6)
        self.assertIn("скорость озвучки ×1.25", "\n".join(done["log"]))

        # Смена скорости пересобирает видео, но не озвучку.
        status, changed = self.json("POST", f"/api/studio/projects/{project['id']}/settings", {"tempo": 1})
        self.assertEqual(status, 200, changed)
        self.assertEqual((self.statuses(changed)["voice"], self.statuses(changed)["render"]), ("done", "pending"))
        bad = self.json("POST", f"/api/studio/projects/{project['id']}/settings", {"tempo": 3})
        self.assertEqual(bad[0], 400)
        self.assertIn("tempo", bad[1]["error"])

        # Слайды дайджеста — подлиннее; время на чтение пересобирает видео, длина слайда — план.
        self.assertEqual((done["settings"]["slide_s"], done["settings"]["reading"]), (8, "normal"))
        status, changed = self.json("POST", f"/api/studio/projects/{project['id']}/settings", {"reading": "slow"})
        self.assertEqual(status, 200, changed)
        self.assertEqual((self.statuses(changed)["plan"], self.statuses(changed)["render"]), ("done", "pending"))
        status, changed = self.json("POST", f"/api/studio/projects/{project['id']}/settings", {"slide_s": 12})
        self.assertEqual(status, 200, changed)
        self.assertEqual(self.statuses(changed)["plan"], "pending")
        for bad_value in ({"slide_s": 7}, {"slide_s": True}, {"reading": "fast"}):
            bad = self.json("POST", f"/api/studio/projects/{project['id']}/settings", bad_value)
            self.assertEqual(bad[0], 400, bad_value)
            self.assertIn(next(iter(bad_value)), bad[1]["error"])

    def test_article_has_no_branding_by_default(self):
        project = self.create(scenario="article", text="Заголовок\n\nПервый абзац. Второе предложение.", run=True)
        self.assertFalse(project["settings"]["branding"])
        self.assertEqual(project["settings"]["tempo"], 1.0)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertFalse((self.folder(project["id"]) / "video-cover.jpg").exists())
        self.assertFalse(any(self.folder(project["id"]).glob("voice-x*")))


@NEEDS_FFMPEG
class DigestChatGptCoverTest(StudioCase):
    def test_poster_waits_for_cover_from_chatgpt(self):
        project = self.create(scenario="digest", day="2026-10-04", run=True)
        self.assertEqual(project["settings"]["poster_mode"], "upload", "обложку дайджеста делают в ChatGPT")
        waiting = self.poll(
            f"/api/studio/projects/{project['id']}",
            lambda p: not p["running"] or self.statuses(p)["poster"] == "waiting",
        )
        poster = next(s for s in waiting["steps"] if s["id"] == "poster")
        self.assertEqual(poster["status"], "waiting")
        folder = Path((self.folder(project["id"]) / "narrator" / "folder.txt").read_text(encoding="utf-8").strip())
        self.assertIn(str(folder), poster["note"])
        self.assertEqual(waiting["artifacts"]["poster_prompt"].strip(), fake_news.IMAGE_PROMPT.strip())
        self.settle(project["id"])

        # Картинку из ChatGPT положили в каталог выпуска narrator — перезапуск шага её подхватывает.
        (folder / "cover.png").write_bytes(png_bytes())
        self.json("POST", f"/api/studio/projects/{project['id']}/run", {})
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["poster"], "done", done["log"])
        self.assertEqual(self.statuses(done)["render"], "done", done["log"])
        self.assertEqual((self.folder(project["id"]) / "poster.png").read_bytes(), png_bytes())


@NEEDS_FFMPEG
class DigestNarratorCoverTest(StudioCase):
    process_env = FAST | {"FAKE_NEWS_COVER": "1"}

    def test_ready_cover_from_narrator_is_the_poster(self):
        project = self.create(scenario="digest", day="2026-10-03", run=True)
        done = self.settle(project["id"])
        self.assertEqual(self.statuses(done)["poster"], "done", done["log"])
        self.assertIn("постер — готовая обложка выпуска narrator", "\n".join(done["log"]))
        folder = self.folder(project["id"])
        self.assertEqual((folder / "poster.png").read_bytes(), (folder / "narrator" / "cover.png").read_bytes())
        _, photos = self.json("GET", "/api/photo/jobs")
        slides_n = sum(s["kind"] == "image" for s in done["artifacts"]["plan"]["slides"])
        self.assertEqual(len(photos["jobs"]), slides_n, "задачи «Фото» — только картинки слайдов, постер не рисовали")


if __name__ == "__main__":
    unittest.main()
