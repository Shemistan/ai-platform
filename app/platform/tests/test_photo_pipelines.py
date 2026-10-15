"""argv и разбор вывода обоих пайплайнов. Образцы вывода — из mflux 0.22.0 и Flux2CLI v3.1.0."""

import unittest
from pathlib import Path

from aiplatform.photo import flux2, mflux, models, params

AVAILABLE = models.resolve(models_dir=Path("/models"), fake=True)
BY_ID = {m.id: m for m in AVAILABLE}
SRC, MASK = "a" * 32, "b" * 32
OUT, SRC_PATH, MASK_PATH = Path("/jobs/x/output.png"), Path("/up/src.png"), Path("/up/mask.png")


def spec_for(**payload):
    sizes = {SRC: (1024, 768), MASK: (1024, 768)}
    return params.build_spec({"prompt": "-кот", "seed": 7, **payload}, AVAILABLE, sizes.get)


def pairs(argv):
    """Флаги со значениями: {'--steps': '9', ...}."""
    return {a: b for a, b in zip(argv, argv[1:], strict=False) if a.startswith("--")}


class MfluxArgvTest(unittest.TestCase):
    def build(self, **payload):
        spec = spec_for(**payload)
        available = BY_ID[spec.model]
        return mflux.build_argv(
            (available.model.command,), available.model, spec, OUT, weights=available.path, source=SRC_PATH
        )

    def test_txt2img(self):
        argv = self.build(model="z-image-turbo", style="photo")
        self.assertEqual(argv[0], "mflux-generate-z-image-turbo")
        self.assertIn("--prompt=-кот, photorealistic photograph, natural lighting, sharp focus", argv)
        flags = pairs(argv)
        self.assertEqual(flags["--model"], "/models/z-image-turbo-mflux-q8")
        self.assertEqual(flags["--base-model"], "z-image-turbo")
        self.assertEqual(
            (flags["--width"], flags["--height"], flags["--steps"], flags["--seed"]), ("1024", "1024", "9", "7")
        )
        self.assertEqual(flags["--output"], str(OUT))
        for absent in ("--guidance", "--image", "--low-ram", "--preset"):
            self.assertNotIn(absent, argv)

    def test_qwen_gets_guidance_and_negative(self):
        argv = self.build(model="qwen-image-2512", guidance=5, negative_prompt="-blur")
        self.assertEqual(pairs(argv)["--guidance"], "5")
        self.assertIn("--negative-prompt=-blur", argv)

    def test_krea_has_no_base_model_and_ideogram_uses_preset(self):
        self.assertNotIn("--base-model", self.build(model="krea-2"))
        argv = self.build(model="ideogram-4", detail="high")
        self.assertEqual(pairs(argv)["--preset"], "V4_QUALITY_48")
        self.assertNotIn("--steps", argv)

    def test_img2img_inverts_strength(self):
        argv = self.build(mode="img2img", source=SRC, model="krea-2", strength=0.75)
        at = argv.index("--image")
        self.assertEqual(argv[at : at + 3], ["--image", str(SRC_PATH), "0.25"])
        self.assertEqual((pairs(argv)["--width"], pairs(argv)["--height"]), ("1024", "768"))
        # Сила изменений 1.0 не должна выключить img2img: mflux при нуле игнорирует картинку.
        self.assertEqual(mflux.source_strength(1.0), 0.05)
        self.assertEqual(mflux.source_strength(0.1), 0.9)

    def test_low_ram(self):
        spec = spec_for(model="krea-2")
        model = BY_ID["krea-2"]
        argv = mflux.build_argv(("x",), model.model, spec, OUT, weights=model.path, low_ram=True)
        self.assertEqual(argv[-1], "--low-ram")


class Flux2ArgvTest(unittest.TestCase):
    def build(self, low_ram=False, **payload):
        spec = spec_for(**payload)
        available = BY_ID[spec.model]
        return flux2.build_argv(
            ("Flux2CLI",), available.model, spec, OUT,
            models_dir=available.path, source=SRC_PATH, mask=MASK_PATH, low_ram=low_ram,
        )  # fmt: skip

    def test_txt2img_dev(self):
        argv = self.build(model="flux2-dev", size="1216x832", low_ram=True)
        self.assertEqual(argv[:4], ["Flux2CLI", "t2i", "--model", "dev"])
        self.assertEqual(argv[-2:], ["--", "-кот"], "промпт позиционный и идёт после «--»")
        flags = pairs(argv)
        self.assertEqual(
            (flags["--width"], flags["--height"], flags["--steps"], flags["--guidance"]), ("1216", "832", "28", "4")
        )
        self.assertEqual((flags["--text-quant"], flags["--transformer-quant"]), ("4bit", "int4"))
        self.assertEqual(flags["--models-dir"], "/models/flux2-swift-mlx")
        self.assertEqual(flags["--memory-profile"], "conservative")

    def test_img2img_klein(self):
        argv = self.build(mode="img2img", source=SRC, model="flux2-klein-9b")
        self.assertEqual(argv[:4], ["Flux2CLI", "i2i", "--model", "klein-9b"])
        flags = pairs(argv)
        self.assertEqual(flags["--images"], str(SRC_PATH))
        self.assertEqual((flags["--guidance"], flags["--steps"], flags["--transformer-quant"]), ("1", "4", "qint8"))
        self.assertNotIn("--strength", argv)

    def test_inpaint(self):
        argv = self.build(mode="inpaint", source=SRC, mask=MASK, model="flux2-klein-9b", strength=0.7)
        self.assertEqual(argv[:4], ["Flux2CLI", "inpaint", "--flux-model", "klein-9b"])
        self.assertIn("--prompt=-кот", argv)
        self.assertIn("--composite-on-original", argv)
        flags = pairs(argv)
        self.assertEqual((flags["--image"], flags["--mask"]), (str(SRC_PATH), str(MASK_PATH)))
        self.assertEqual((flags["--strength"], flags["--max-pixels"]), ("0.7", str(1024 * 768)))
        self.assertEqual(flags["--output"], str(OUT))

    def test_outpaint_has_no_quant_flags(self):
        argv = self.build(mode="outpaint", source=SRC, padding={"left": 128, "bottom": 64})
        self.assertEqual(argv[:4], ["Flux2CLI", "outpaint", "--flux-model", "klein-9b"])
        flags = pairs(argv)
        self.assertEqual(
            (flags["--left"], flags["--top"], flags["--right"], flags["--bottom"]), ("128", "0", "0", "64")
        )
        for absent in ("--text-quant", "--transformer-quant", "--mask", "--width"):
            self.assertNotIn(absent, argv, "у настоящего outpaint этих флагов нет")


class MfluxProgressTest(unittest.TestCase):
    def test_step_bars_drive_progress(self):
        parser = mflux.ProgressParser()
        self.assertEqual(parser.snapshot()["stage"], "Загрузка модели")
        self.assertFalse(parser.feed("Some warning from the pipeline"))
        self.assertTrue(parser.feed("  0%|          | 0/9 [00:00<?, ?it/s]"))
        self.assertTrue(parser.feed(" 44%|████▍     | 4/9 [00:12<00:15,  3.05s/it]"))
        snap = parser.snapshot()
        self.assertEqual((snap["stage"], snap["step"], snap["steps"]), ("Генерация", 4, 9))
        self.assertTrue(12 < snap["percent"] < 92)
        parser.feed("100%|██████████| 9/9 [00:27<00:00,  3.05s/it]")
        snap = parser.snapshot()
        self.assertEqual((snap["stage"], snap["step"], snap["percent"]), ("Декодирование и сохранение", None, 95.0))

    def test_download_bars_do_not_move_percent(self):
        parser = mflux.ProgressParser()
        before = parser.snapshot()["percent"]
        self.assertTrue(parser.feed("Fetching 12 files:  33%|███▎      | 4/12 [00:10<00:20,  2.5s/it]"))
        self.assertEqual(parser.snapshot()["stage"], "Fetching 12 files — 33%")
        self.assertTrue(parser.feed("model.safetensors:  45%|████▌     | 2.6G/5.9G [01:00<01:10, 46MB/s]"))
        self.assertEqual(parser.snapshot()["percent"], before)
        self.assertIsNone(parser.snapshot()["steps"])


class Flux2ProgressTest(unittest.TestCase):
    def test_stages_and_steps(self):
        parser = flux2.ProgressParser()
        self.assertFalse(parser.feed("Generating 512x512, 4 steps, guidance 1.0, seed 7..."))
        self.assertFalse(parser.feed("Loading transformer..."))
        self.assertEqual(parser.snapshot()["stage"], "Загрузка модели")
        self.assertTrue(parser.feed("Step 2/4 1840ms"))
        snap = parser.snapshot()
        self.assertEqual((snap["stage"], snap["step"], snap["steps"], snap["percent"]), ("Генерация", 2, 4, 52.0))
        parser.feed("Denoising complete, decoding image...")
        self.assertEqual(parser.snapshot()["percent"], 95.0)
        parser.feed("Output saved to: /x/output.png")
        self.assertEqual(parser.snapshot()["percent"], 99.0)

    def test_percent_never_goes_back(self):
        parser = flux2.ProgressParser()
        parser.feed("Step 3/4 10ms")
        high = parser.snapshot()["percent"]
        parser.feed("Loading text encoder...")
        self.assertEqual(parser.snapshot()["percent"], high)


if __name__ == "__main__":
    unittest.main()
