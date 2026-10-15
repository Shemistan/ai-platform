import unittest
from pathlib import Path

from aiplatform.fakes import fake_ltx
from aiplatform.video import living, ltx
from aiplatform.video.timeline import ValidationError
from tests.test_video_server import ServerCase, load


def fake_models():
    return load({"AIP_BACKEND": "fake"}).models()


class SpecTest(unittest.TestCase):
    def test_photo_sits_in_the_middle_of_eight_seconds(self):
        model = living.pick_model(fake_models())
        spec = living.build_spec({"upload_id": "a", "orientation": "portrait"}, model)
        self.assertEqual((spec.duration_s, spec.num_frames, spec.fps), (8, 193, 24))
        self.assertEqual([(k.upload_id, k.frame_idx) for k in spec.keyframes], [("a", 96)])
        self.assertEqual((spec.width, spec.height, spec.audio, spec.kind), (320, 512, False, "living"))

        argv = ltx.build_argv(("x",), spec, [Path("a.png")], Path("o.mp4"), model=model.path)
        args = fake_ltx._parser().parse_args(argv[1:])
        self.assertEqual((args.frames, args.image, args.no_audio), (193, [["a.png", "96", "1.0"]], True))

    def test_bad_requests(self):
        model = living.pick_model(fake_models())
        bad = (None, {}, {"upload_id": ""}, {"upload_id": 5}, {"upload_id": "a", "orientation": "square"})
        for payload in (*bad, {"upload_id": "a", "quality": "xl"}):
            with self.assertRaises(ValidationError, msg=payload):
                living.build_spec(payload, model)

    def test_orientation_and_quality_set_the_frame_size(self):
        model = living.pick_model(fake_models())
        sizes = {
            (o, q): (spec.width, spec.height)
            for o in living.ORIENTATIONS
            for q in living.QUALITIES
            for spec in [living.build_spec({"upload_id": "a", "orientation": o, "quality": q}, model)]
        }
        self.assertEqual(sizes["landscape", "s"], (512, 320))
        self.assertEqual(sizes["portrait", "m"], (448, 704))
        self.assertEqual(sizes["landscape", "l"], (960, 576))
        self.assertEqual(sizes["portrait", "l"], (576, 960))
        self.assertTrue(all(w % 64 == 0 and h % 64 == 0 for w, h in sizes.values()), sizes)

    def test_model_must_take_a_keyframe_and_eight_seconds(self):
        available = fake_models()
        self.assertEqual(living.pick_model(available).id, "ltx-2.3-q4")
        wan_only = [m for m in available if m.pipeline == "wan"]
        with self.assertRaisesRegex(ValidationError, r"setup-model\.sh"):
            living.pick_model(wan_only)
        self.assertIsNone(living.describe(wan_only)["default_model"])
        nothing = load({"AIP_MODELS_DIR": "/nonexistent-models"}).models()
        self.assertIsNone(living.describe(nothing)["default_model"], "ничего не установлено")

    def test_model_from_request(self):
        available = fake_models()
        self.assertEqual(living.pick_model(available, "ltx-2.5-q4").id, "ltx-2.5-q4")
        for model_id, reason in (("нет такой", "Неизвестная"), ("wan-2.2-t2v-q4", "не умеет"), (5, "строкой")):
            with self.assertRaisesRegex(ValidationError, reason):
                living.pick_model(available, model_id)
        with self.assertRaisesRegex(ValidationError, r"setup-model\.sh ltx-2\.5-q4"):
            living.pick_model(load({"AIP_MODELS_DIR": "/nonexistent-models"}).models(), "ltx-2.5-q4")


class LivingServerTest(ServerCase):
    def test_photo_becomes_a_job_that_runs_to_done(self):
        _, cfg = self.json("GET", "/api/video/config")
        self.assertEqual((cfg["living"]["before_s"], cfg["living"]["after_s"]), (4, 4))
        self.assertEqual((cfg["living"]["default_model"], cfg["living"]["problem"]), ("ltx-2.3-q4", None))
        self.assertEqual([m["id"] for m in cfg["living"]["models"]], ["ltx-2.3-q4", "ltx-2.5-q4"])
        self.assertEqual([o["id"] for o in cfg["living"]["orientations"]], ["landscape", "portrait"])
        self.assertEqual(cfg["living"]["qualities"][1]["sizes"], {"landscape": [704, 448], "portrait": [448, 704]})

        photo = self.upload()
        request = {"upload_id": photo, "orientation": "portrait", "quality": "m", "model": "ltx-2.5-q4"}
        status, job = self.json("POST", "/api/video/living", request)
        self.assertEqual(status, 201, job)
        self.assertEqual((job["kind"], job["duration_s"], job["num_frames"], job["audio"]), ("living", 8, 193, False))
        self.assertEqual((job["width"], job["height"], job["model"]), (448, 704, "ltx-2.5-q4"))
        self.assertEqual([(f["upload_id"], f["frame_idx"], f["time_s"]) for f in job["frames"]], [(photo, 96, 4.0)])
        done, _ = self.wait(job["id"])
        self.assertEqual(done["status"], "done", done)
        self.assertEqual(self.call("GET", done["video_url"])[0], 200)

        _, plain = self.json("POST", "/api/video/jobs", {"prompt": "кот"})
        self.assertEqual(plain["kind"], "video")

    def test_bad_requests(self):
        self.assertEqual(self.json("POST", "/api/video/living", {})[0], 400)
        status, body = self.json("POST", "/api/video/living", {"upload_id": "0" * 32})
        self.assertEqual(status, 400)
        self.assertIn("не найдена", body["error"])
        self.assertEqual(self.json("POST", "/api/video/living", {"upload_id": "x", "model": "wan-2.2-t2v-q4"})[0], 400)
        status, _ = self.json("POST", "/api/video/living", {"upload_id": "x"}, {"Origin": "https://evil.example"})
        self.assertEqual(status, 403)


if __name__ == "__main__":
    unittest.main()
