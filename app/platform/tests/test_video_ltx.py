import unittest
from pathlib import Path

from aiplatform.fakes import fake_ltx
from aiplatform.video import ltx
from aiplatform.video.timeline import build_spec


class BuildArgvTest(unittest.TestCase):
    def test_keyframes_go_in_order_with_positions(self):
        spec = build_spec(
            {"prompt": "закат", "seed": 7, "audio": False, "frames": [{"upload_id": "a"}, {"upload_id": "b"}]}
        )
        argv = ltx.build_argv(
            ("ltx-2-mlx",), spec, [Path("/u/a.png"), Path("/u/b.jpg")], Path("/out/o.mp4"), model="repo/q4"
        )
        self.assertEqual(argv[:3], ["ltx-2-mlx", "generate", "--distilled"])
        self.assertIn("--no-audio", argv)
        self.assertNotIn("--low-ram", argv)
        images = [argv[i + 1 : i + 4] for i, a in enumerate(argv) if a == "--image"]
        self.assertEqual(images, [["/u/a.png", "0", "1.0"], ["/u/b.jpg", "96", "1.0"]])

    def test_argv_is_accepted_by_cli_contract(self):
        """fake_ltx повторяет флаги настоящего CLI — argv должен разбираться без ошибок."""
        spec = build_spec({"prompt": "-p с дефисом в начале", "frames": [{"upload_id": "a"}]})
        argv = ltx.build_argv(("x",), spec, [Path("a.png")], Path("o.mp4"), model="m", gemma="/g g", low_ram=True)
        self.assertEqual(argv[argv.index("--gemma") + 1], "/g g")
        args = fake_ltx._parser().parse_args(argv[1:])
        self.assertEqual(args.prompt, "-p с дефисом в начале")
        self.assertEqual((args.width, args.height, args.frames, args.frame_rate), (512, 320, 97, 24.0))
        self.assertEqual(args.image, [["a.png", "0", "1.0"]])
        self.assertTrue(args.low_ram)

    def test_image_count_mismatch(self):
        spec = build_spec({"prompt": "p", "frames": [{"upload_id": "a"}]})
        with self.assertRaises(ValueError):
            ltx.build_argv(("x",), spec, [], Path("o.mp4"), model="m")


def bar(n, total, desc="Denoising"):
    return f"{desc}:  {round(100 * n / total)}%|███▊      | {n}/{total} [00:40<01:05, 13.2s/it]"


class ProgressParserTest(unittest.TestCase):
    def test_full_distilled_run(self):
        p = ltx.ProgressParser()
        seen = []

        def feed(line):
            p.feed(line)
            seen.append(p.percent)

        feed("Mode: Distilled Two-Stage (half-res + upscale + distilled refine)")
        feed("[Encoding prompt] ...")
        self.assertEqual(p.stage, "Кодирование промпта")
        feed("[Encoding prompt] done in 3.1s")
        for n in range(9):
            feed(bar(n, 8))
        self.assertEqual((p.stage, p.step, p.steps), ("Шумоподавление, проход 1", 8, 8))
        self.assertEqual(p.percent, 60.0)
        for n in range(4):
            feed(bar(n, 3))
        self.assertEqual((p.stage, p.step, p.steps), ("Шумоподавление, проход 2", 3, 3))
        self.assertEqual(p.percent, 85.0)
        feed("[Loading decoders (VAE + audio + vocoder)] ...")
        self.assertEqual(p.stage, "Загрузка декодеров")
        self.assertEqual(seen, sorted(seen), "процент не должен убывать")
        self.assertLess(p.percent, 100)

    def test_bar_lines_are_flagged_for_log_filtering(self):
        p = ltx.ProgressParser()
        self.assertTrue(p.feed(bar(1, 8)))
        self.assertFalse(p.feed("[Encoding prompt] ..."))
        self.assertFalse(p.feed("Traceback (most recent call last):"))

    def test_download_bar_shows_stage_but_not_percent(self):
        p = ltx.ProgressParser()
        p.feed("Fetching 12 files:  25%|██▌       | 3/12 [00:10<00:30,  3.3s/it]")
        self.assertEqual(p.stage, "Fetching 12 files — 25%")
        self.assertEqual(p.percent, 0.0)
        p.feed("transformer-distilled.safetensors:  45%|████▌     | 5.1G/11.3G [01:00<01:10, 88MB/s]")
        self.assertIn("45%", p.stage)
        self.assertEqual(p.percent, 0.0)

    def test_phases_before_denoise_stay_under_first_pass(self):
        p = ltx.ProgressParser()
        for i in range(20):
            p.feed(f"[Phase {i}] ...")
        self.assertLess(p.percent, 10.0)


if __name__ == "__main__":
    unittest.main()
