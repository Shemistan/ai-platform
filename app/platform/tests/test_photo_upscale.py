"""Увеличение до 2K/4K: расчёт размеров, argv SeedVR2, сквозной прогон через fake_gen."""

import unittest
from pathlib import Path

from aiplatform.core import uploads
from aiplatform.photo import upscale
from aiplatform.photo.params import ValidationError
from tests.test_photo_server import ServerCase


class SizesTest(unittest.TestCase):
    def test_long_side_hits_target(self):
        cases = {
            (1024, 1024, "2k"): (2048, 2048),
            (1024, 1024, "4k"): (4096, 4096),
            (1344, 768, "2k"): (2048, 1170),
            (768, 1344, "4k"): (2340, 4096),
            (1216, 832, "2k"): (2048, 1402),
        }
        for (w, h, target), size in cases.items():
            with self.subTest(w=w, h=h, target=target):
                spec = upscale.build_spec({"target": target}, "p", w, h, 7)
                self.assertEqual((spec.width, spec.height), size)
                self.assertIn(spec.resolution - min(size), (0, 1), "SeedVR2 отбросит нечётный пиксель сам")

    def test_targets_only_enlarge(self):
        self.assertEqual(upscale.targets_for(1024, 1024), ["2k", "4k"])
        self.assertEqual(upscale.targets_for(2048, 1170), ["4k"])
        self.assertEqual(upscale.targets_for(4096, 2340), [])

    def test_bad_target(self):
        for payload in ({}, {"target": "8k"}, None, {"target": 2}):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                upscale.build_spec(payload, "p", 1024, 1024, 1)
        with self.assertRaisesRegex(ValidationError, "уже не меньше 2K"):
            upscale.build_spec({"target": "2k"}, "p", 2048, 2048, 1)

    def test_argv(self):
        spec = upscale.build_spec({"target": "2k"}, "p", 1344, 768, 42)
        argv = upscale.build_argv(("mflux-upscale-seedvr2",), spec, Path("/in.png"), Path("/out.png"), low_ram=True)
        self.assertEqual(
            argv,
            [
                "mflux-upscale-seedvr2", "--model", "seedvr2-3b", "--image-path", "/in.png",
                "--resolution", "1171", "--seed", "42", "--vae-tiling", "--output", "/out.png", "--low-ram",
            ],
        )  # fmt: skip


class UpscaleServerTest(ServerCase):
    def test_upscale_generated_image(self):
        done, _, _ = self.run_job({"prompt": "кот", "model": "z-image-turbo", "size": "768x768"})
        self.assertEqual(done["upscale_targets"], ["2k", "4k"])

        status, job = self.json("POST", f"/api/photo/jobs/{done['id']}/upscale", {"target": "2k"})
        self.assertEqual(status, 201, job)
        self.assertEqual((job["mode"], job["model_label"]), ("upscale", "SeedVR2 3B"))
        self.assertEqual((job["width"], job["height"], job["source_size"]), (2048, 2048, [768, 768]))
        self.assertEqual((job["parent_id"], job["prompt"], job["seed"]), (done["id"], "кот", done["seed"]))
        self.assertEqual(job["source_url"], done["image_url"])

        big, _ = self.wait(job["id"])
        self.assertEqual(big["status"], "done", big)
        self.assertEqual(big["progress"]["percent"], 100.0)
        self.assertEqual(big["upscale_targets"], ["4k"], "2K можно поднять до 4K, но не до 2K снова")
        _, _, image = self.call("GET", big["image_url"])
        self.assertEqual(uploads.png_size(image), (2048, 2048))

    def test_upscale_errors(self):
        status, _ = self.json("POST", "/api/photo/jobs/000000000000/upscale", {"target": "2k"})
        self.assertEqual(status, 404)

        done, _, _ = self.run_job({"prompt": "p", "size": "512x512"})
        status, err = self.json("POST", f"/api/photo/jobs/{done['id']}/upscale", {"target": "8k"})
        self.assertEqual(status, 400)
        self.assertIn("2K или 4K", err["error"])

    def test_config_describes_upscale(self):
        _, cfg = self.json("GET", "/api/photo/config")
        self.assertTrue(cfg["upscale"]["available"])
        self.assertEqual([t["id"] for t in cfg["upscale"]["targets"]], ["2k", "4k"])


class UnfinishedTest(ServerCase):
    step_delay = "0.3"

    def test_unfinished_job_is_not_upscaled(self):
        status, job = self.json("POST", "/api/photo/jobs", {"prompt": "p", "size": "512x512"})
        self.assertEqual(status, 201)
        self.assertEqual(job["upscale_targets"], [])
        status, err = self.json("POST", f"/api/photo/jobs/{job['id']}/upscale", {"target": "2k"})
        self.assertEqual(status, 400)
        self.assertIn("только готовую", err["error"])
        self.json("POST", f"/api/photo/jobs/{job['id']}/cancel")
        self.wait(job["id"])


if __name__ == "__main__":
    unittest.main()
