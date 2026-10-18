"""Вкладка «Диктор» (PRD-017): персонажи, argv SadTalker, задача текст → озвучка → видео на фейках."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from aiplatform.audio.service import AudioModule
from aiplatform.avatar import models, sadtalker
from aiplatform.avatar.characters import CharacterError, CharacterStore, consent_of
from aiplatform.avatar.config import AvatarConfig
from aiplatform.avatar.jobs import NO_FACE
from aiplatform.avatar.service import AvatarModule
from aiplatform.core import media
from aiplatform.fakes import fake_sadtalker
from tests.fixtures import FAST, NEEDS_FFMPEG, png
from tests.helpers import PlatformCase

CONSENT = {"confirmed": True, "owner": "Шамистан", "form": "self"}
TEXT = "Привет! Это короткий выпуск новостей."


def avatar_modules(ctx):
    return [AudioModule(ctx), AvatarModule(ctx)]


def config(root: Path, fake: bool = False) -> AvatarConfig:
    return AvatarConfig(root / "data", "fake" if fake else "sadtalker", root / "home", root / "models", "ffmpeg")


class ArgvTest(unittest.TestCase):
    def spec(self, frame="crop", enhance=False, size=256):
        return sadtalker.RenderSpec(Path("s.wav"), Path("face.png"), size, frame, enhance)

    def test_crop_is_head_close_up(self):
        argv = sadtalker.build_argv(("py", "inference.py"), self.spec(), Path("out"), Path("w"))
        self.assertEqual(argv[:2], ["py", "inference.py"])
        args = fake_sadtalker._parser().parse_args(argv[2:])
        self.assertEqual((args.preprocess, args.still, args.enhancer, args.size), ("crop", False, None, 256))
        self.assertEqual((args.checkpoint_dir, args.cpu), (str(Path("w") / "checkpoints"), True))

    def test_full_keeps_the_photo_and_still_head(self):
        argv = sadtalker.build_argv(("py", "i.py"), self.spec("full", True, 512), Path("out"), Path("w"))
        args = fake_sadtalker._parser().parse_args(argv[2:])
        self.assertEqual((args.preprocess, args.still, args.enhancer, args.size), ("full", True, "gfpgan", 512))


class ProgressTest(unittest.TestCase):
    def test_bars_move_progress_and_result_is_remembered(self):
        parser = sadtalker.ProgressParser()
        self.assertTrue(parser.feed("landmark Det:: 100%|██████████| 1/1 [00:00<00:00,  2.10it/s]"))
        self.assertEqual(parser.stage, "Поиск лица")
        parser.feed("Face Renderer::  50%|█████     | 5/10 [00:10<00:10,  1.00s/it]")
        self.assertEqual((parser.stage, parser.step, parser.steps, parser.percent), ("Анимация", 5, 10, 57.5))
        parser.feed("mel:: 100%|██████████| 3/3 [00:00<00:00]")  # назад прогресс не идёт
        self.assertEqual(parser.percent, 57.5)
        self.assertFalse(parser.feed("The generated video is named: /x/res/2026_10_18_10.00.00.mp4"))
        self.assertEqual(parser.result, Path("/x/res/2026_10_18_10.00.00.mp4"))
        self.assertFalse(parser.no_face)
        parser.feed("Can't get the coeffs of the input")
        self.assertTrue(parser.no_face)


class InstalledTest(unittest.TestCase):
    def test_needs_environment_and_every_weight(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cfg = config(root)
            model = models.CATALOG[0]
            self.assertFalse(models.installed(model, cfg))
            self.assertEqual(len(models.missing(cfg)), 2)
            for part in cfg.sadtalker_cmd():
                Path(part).parent.mkdir(parents=True, exist_ok=True)
                Path(part).touch()
            self.assertEqual(models.missing(cfg), [f"весов в {cfg.weights}"])
            for name in models.SADTALKER_FILES:
                (cfg.weights / name).parent.mkdir(parents=True, exist_ok=True)
                (cfg.weights / name).touch()
            self.assertTrue(models.installed(model, cfg))
            self.assertEqual(models.missing(cfg), [])
            self.assertTrue(models.installed(model, config(Path("/nonexistent"), fake=True)))


class CharacterTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.store = CharacterStore(self.root / "chars", "/api/avatar")

    def upload(self) -> Path:
        path = self.root / "u.png"
        path.write_bytes(png())
        return path

    def test_photo_needs_consent_drawn_does_not(self):
        with self.assertRaisesRegex(CharacterError, "согласие"):
            self.store.create({"name": "Я", "kind": "photo"}, self.upload())
        drawn = self.store.create({"name": "  Кот  Учёный ", "kind": "drawn"}, self.upload())
        self.assertEqual((drawn["name"], drawn["kind_label"]), ("Кот Учёный", "Рисованный"))
        self.assertFalse((self.root / "u.png").exists(), "картинка переехала в каталог персонажа")
        photo = self.store.create({"name": "Я", "kind": "photo", "consent": CONSENT}, self.upload())
        self.assertNotIn("consent", photo, "согласие не отдаётся в API")
        saved = json.loads((self.root / "chars" / photo["id"] / "character.json").read_text())
        self.assertEqual(saved["consent"]["owner"], "Шамистан")
        self.assertEqual([c["id"] for c in self.store.list()], [drawn["id"], photo["id"]])

    def test_rename_delete_and_bad_input(self):
        item = self.store.create({"name": "А", "kind": "drawn"}, self.upload())
        self.assertEqual(self.store.rename(item["id"], {"name": "Б"})["name"], "Б")
        self.assertIsNone(self.store.rename("0" * 12, {"name": "В"}))
        self.assertIsNone(self.store.get("../etc"))
        self.assertTrue(self.store.delete(item["id"]))
        self.assertFalse(self.store.delete(item["id"]))
        for payload, source in (
            ({"name": "", "kind": "drawn"}, self.upload()),
            ({"name": "x" * 81, "kind": "drawn"}, self.upload()),
            ({"name": "А", "kind": "robot"}, self.upload()),
            ({"name": "А", "kind": "drawn"}, None),
        ):
            with self.subTest(payload=payload), self.assertRaises(CharacterError):
                self.store.create(payload, source)

    def test_consent_forms(self):
        self.assertEqual(consent_of(CONSENT | {"owner": " Анна  Ли "})["owner"], "Анна Ли")
        for bad in (None, {}, CONSENT | {"confirmed": "yes"}, CONSENT | {"owner": ""}, CONSENT | {"form": "oral"}):
            with self.subTest(bad=bad), self.assertRaises(CharacterError):
                consent_of(bad)


@NEEDS_FFMPEG
class AvatarServerTest(PlatformCase):
    modules = staticmethod(avatar_modules)
    process_env = FAST | {"FAKE_SADTALKER_STEP_DELAY": "0.01", "FAKE_SADTALKER_NO_FACE": ""}

    def character(self, kind="photo", name="Я"):
        status, upload = self.json("POST", "/api/avatar/uploads", png(96, 96, 3))
        self.assertEqual(status, 201, upload)
        payload = {"upload_id": upload["id"], "name": name, "kind": kind}
        if kind == "photo":
            payload["consent"] = CONSENT
        status, item = self.json("POST", "/api/avatar/characters", payload)
        self.assertEqual(status, 201, item)
        return item

    def wait(self, job_id):
        return self.wait_job(f"/api/avatar/jobs/{job_id}", timeout=60)[0]

    def test_config(self):
        status, cfg = self.json("GET", "/api/avatar/config")
        self.assertEqual(status, 200)
        self.assertEqual((cfg["default_model"], cfg["backend"], cfg["audio_available"]), ("sadtalker", "fake", True))
        self.assertEqual(cfg["models"][0]["sizes"], [256, 512])
        self.assertEqual([f["id"] for f in cfg["frames"]], ["crop", "full"])

    def test_text_becomes_talking_video(self):
        char = self.character()
        audit = [json.loads(line) for line in (Path(self.tmp.name) / "audit.jsonl").read_text().splitlines()]
        record = [(a["action"], a["owner"], a["form"]) for a in audit]
        self.assertEqual(record, [("likeness_consent", "Шамистан", "self")])
        self.assertEqual(len(audit[0]["image_sha256"]), 64)

        request = {"character": char["id"], "text": TEXT, "engine": "silero", "voice": "baya", "frame": "full"}
        status, job = self.json("POST", "/api/avatar/jobs", request)
        self.assertEqual(status, 201, job)
        picked = (job["character"]["name"], job["voice"]["voice"], job["frame"])
        self.assertEqual(picked, ("Я", "baya", "full"))
        done, seen = self.wait_job(f"/api/avatar/jobs/{job['id']}", timeout=60)
        self.assertEqual(done["status"], "done", done)
        stages = {s["progress"]["stage"] for s in seen}
        self.assertTrue(any(s.startswith("Озвучка") for s in stages), stages)
        self.assertGreater(done["duration_s"], 0)
        self.assertIsNotNone(done["audio_job"])

        status, headers, video = self.call("GET", done["video_url"] + "?download=1")
        self.assertEqual((status, headers["Content-Type"]), (200, "video/mp4"))
        self.assertIn("attachment", headers["Content-Disposition"])
        path = Path(self.tmp.name) / "avatar" / "jobs" / job["id"] / "output.mp4"
        entries = "format_tags=comment:stream=codec_type"
        argv = [media.ffprobe_for(), "-v", "error", "-show_entries", entries, "-of", "json", str(path)]
        probe = subprocess.run(argv, capture_output=True, text=True, check=True)
        info = json.loads(probe.stdout)
        self.assertEqual(info["format"]["tags"]["comment"], "AI-generated")
        self.assertEqual(sorted(s["codec_type"] for s in info["streams"]), ["audio", "video"])
        self.assertFalse((path.parent / "sadtalker").exists(), "промежуточный каталог SadTalker убран")

        # Персонажа удалили — история задачи с его картинкой жива.
        self.assertEqual(self.json("POST", f"/api/avatar/characters/{char['id']}/delete")[0], 200)
        self.assertEqual(self.call("GET", done["face_url"])[0], 200)
        _, listing = self.json("GET", "/api/avatar/jobs")
        self.assertEqual([j["id"] for j in listing["jobs"]], [job["id"]])

    def test_own_audio(self):
        char = self.character(kind="drawn", name="Кот")
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "v.wav"
            subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=2",
                            str(wav)], check=True)  # fmt: skip
            status, upload = self.json("POST", "/api/avatar/uploads", wav.read_bytes())
        self.assertEqual(status, 201, upload)
        request = {"character": char["id"], "source": "audio", "upload_id": upload["id"], "size": 512}
        status, job = self.json("POST", "/api/avatar/jobs", request)
        self.assertEqual(status, 201, job)
        self.assertIsNone(job["voice"])
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertAlmostEqual(done["duration_s"], 2.0, delta=0.2)
        self.assertIsNone(done["audio_job"])

    def test_characters_api(self):
        char = self.character(kind="drawn", name="Робот")
        _, listing = self.json("GET", "/api/avatar/characters")
        self.assertEqual([c["name"] for c in listing["characters"]], ["Робот"])
        status, headers, _ = self.call("GET", char["image_url"])
        self.assertEqual((status, headers["Content-Type"]), (200, "image/png"))
        status, renamed = self.json("POST", f"/api/avatar/characters/{char['id']}/rename", {"name": "Робот-2"})
        self.assertEqual((status, renamed["name"]), (200, "Робот-2"))
        self.assertEqual(self.json("POST", f"/api/avatar/characters/{char['id']}/rename", {"name": ""})[0], 400)
        self.assertEqual(self.json("POST", "/api/avatar/characters/0123456789ab/delete")[0], 404)
        status, upload = self.json("POST", "/api/avatar/uploads", png())
        no_consent = {"upload_id": upload["id"], "name": "Я", "kind": "photo"}
        status, body = self.json("POST", "/api/avatar/characters", no_consent)
        self.assertEqual(status, 400)
        self.assertIn("согласие", body["error"])

    def test_bad_job_requests(self):
        char = self.character(kind="drawn")
        good = {"character": char["id"], "text": TEXT, "engine": "silero"}
        for bad, reason in (
            ({**good, "character": "0" * 12}, "персонажа"),
            ({**good, "model": "wav2lip"}, "Неизвестная модель"),
            ({**good, "size": 1024}, "Качество"),
            ({**good, "frame": "body"}, "Кадр"),
            ({**good, "enhance": "да"}, "enhance"),
            ({**good, "text": ""}, "текст"),
            ({**good, "engine": "nope"}, "движок"),
            ({**good, "source": "audio"}, "озвучку"),
            ({**good, "source": "video"}, "Речь"),
        ):
            with self.subTest(bad=bad):
                status, body = self.json("POST", "/api/avatar/jobs", bad)
                self.assertEqual(status, 400, body)
                self.assertIn(reason, body["error"])
        status, _ = self.json("POST", "/api/avatar/jobs", good, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)

    def test_cancel(self):
        char = self.character(kind="drawn")
        long_text = {"character": char["id"], "text": TEXT * 20, "engine": "silero"}
        status, job = self.json("POST", "/api/avatar/jobs", long_text)
        self.assertEqual(status, 201, job)
        self.assertEqual(self.json("POST", f"/api/avatar/jobs/{job['id']}/cancel")[0], 200)
        done = self.wait(job["id"])
        self.assertEqual(done["status"], "canceled", done)
        self.assertIsNone(self.json("GET", f"/api/avatar/jobs/{job['id']}")[1]["video_url"])


@NEEDS_FFMPEG
class NoFaceTest(PlatformCase):
    modules = staticmethod(avatar_modules)
    process_env = FAST | {"FAKE_SADTALKER_STEP_DELAY": "0.01", "FAKE_SADTALKER_NO_FACE": "1"}

    def test_no_face_is_a_clear_error(self):
        _, upload = self.json("POST", "/api/avatar/uploads", png(96, 96, 3))
        drawn = {"upload_id": upload["id"], "name": "Лес", "kind": "drawn"}
        _, char = self.json("POST", "/api/avatar/characters", drawn)
        _, job = self.json("POST", "/api/avatar/jobs", {"character": char["id"], "text": TEXT, "engine": "silero"})
        done = self.wait_job(f"/api/avatar/jobs/{job['id']}", timeout=60)[0]
        self.assertEqual((done["status"], done["error"]), ("failed", NO_FACE))


class ProblemsTest(PlatformCase):
    modules = staticmethod(avatar_modules)
    extra_env = {"AIP_BACKEND": "", "AIP_AVATAR_SADTALKER_HOME": "/nonexistent-sadtalker"}

    def test_missing_model_is_a_problem_and_job_is_refused(self):
        module = self.app.modules["avatar"]
        problems = module.problems()
        self.assertEqual(len(problems), 1)
        self.assertIn("make models M=sadtalker", problems[0])
        self.assertFalse(module.describe()["models"][0]["installed"])


if __name__ == "__main__":
    unittest.main()
