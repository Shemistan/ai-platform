import unittest
from pathlib import Path

from aiplatform.fakes import fake_wan
from aiplatform.video import models, wan
from aiplatform.video.timeline import build_spec

LIMITS = next(m for m in models.CATALOG if m.pipeline == "wan").limits


class BuildArgvTest(unittest.TestCase):
    def test_argv_is_accepted_by_cli_contract(self):
        spec = build_spec({"prompt": "-p с дефисом в начале", "duration_s": 5, "seed": 7}, LIMITS)
        argv = wan.build_argv(("wan",), spec, Path("/out/v.mp4"), model="/m/q4", steps=10)
        args = fake_wan._parser().parse_args(argv[1:])
        self.assertEqual((args.prompt, args.model_dir, args.output_path), (spec.prompt, "/m/q4", "/out/v.mp4"))
        self.assertEqual((args.width, args.height, args.num_frames, args.steps, args.seed), (512, 320, 81, 10, 7))

    def test_keyframes_are_refused(self):
        spec = build_spec({"prompt": "p", "frames": [{"upload_id": "a"}]})
        with self.assertRaises(ValueError):
            wan.build_argv(("wan",), spec, Path("o.mp4"), model="m", steps=10)


class ProgressParserTest(unittest.TestCase):
    def test_full_run(self):
        parser = wan.ProgressParser()
        seen = []
        for line in (
            "  Size: 512x320, Frames: 81",
            "Loading T5 encoder...",
            "Encoding text...",
            "  T5 encoding: 1.6s",
            "Loading transformer model(s)...",
            "  Using 4-bit quantized weights (group_size=64)",
            "Denoising (10 steps)...",
            "Diffusion:   0%|          | 0/10 [00:00<?, ?it/s]",
            "Diffusion:  50%|█████     | 5/10 [04:30<04:30, 54.0s/it]",
            "Diffusion: 100%|██████████| 10/10 [09:06<00:00, 54.6s/it]",
            "Decoding with VAE...",
            "✓ Video saved to out.mp4",
        ):
            is_bar = parser.feed(line)
            self.assertEqual(is_bar, line.startswith("Diffusion"), line)
            seen.append(parser.snapshot())
        percents = [s["percent"] for s in seen]
        self.assertEqual(percents, sorted(percents))
        self.assertEqual((seen[8]["stage"], seen[8]["step"], seen[8]["steps"]), ("Шумоподавление", 5, 10))
        self.assertEqual(seen[-1]["stage"], "Декодирование видео")
        self.assertLess(seen[-1]["percent"], 100)


if __name__ == "__main__":
    unittest.main()
