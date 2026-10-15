"""Проверка запроса: режимы, возможности моделей, размеры."""

import tempfile
import unittest
from pathlib import Path

from aiplatform.photo import models, params

AVAILABLE = models.resolve(models_dir=Path("/models"), fake=True)
SRC, MASK = "a" * 32, "b" * 32
SIZES = {SRC: (1024, 768), MASK: (1024, 768)}


def build(**payload):
    return params.build_spec({"prompt": "кот", **payload}, AVAILABLE, SIZES.get)


class Txt2ImgTest(unittest.TestCase):
    def test_defaults(self):
        spec = build()
        self.assertEqual((spec.mode, spec.model, spec.width, spec.height), ("txt2img", "flux2-dev", 1024, 1024))
        self.assertEqual((spec.steps, spec.guidance, spec.seed, spec.strength), (28, 4.0, -1, None))
        self.assertEqual(spec.full_prompt(), "кот")

    def test_style_detail_and_size(self):
        spec = build(model="z-image-turbo", style="watercolor", detail="high", size="1344x768")
        self.assertEqual((spec.width, spec.height, spec.steps, spec.guidance), (1344, 768, 12, None))
        self.assertEqual(spec.full_prompt(), "кот, watercolor painting, soft washes, paper texture")

    def test_custom_steps_and_guidance(self):
        spec = build(model="qwen-image-2512", steps=25, guidance=5, negative_prompt=" размыто ")
        self.assertEqual((spec.steps, spec.guidance, spec.negative_prompt), (25, 5.0, "размыто"))
        for bad in ({"steps": 0}, {"steps": 51}, {"steps": 2.5}, {"guidance": 11}, {"guidance": "4"}):
            with self.assertRaises(params.ValidationError, msg=bad):
                build(model="qwen-image-2512", **bad)

    def test_ideogram_takes_preset_instead_of_steps(self):
        spec = build(model="ideogram-4", detail="draft")
        self.assertEqual((spec.preset, spec.steps), ("V4_TURBO_12", 12))
        with self.assertRaisesRegex(params.ValidationError, "уровень детализации"):
            build(model="ideogram-4", steps=30)

    def test_knobs_the_model_ignores_are_refused(self):
        with self.assertRaisesRegex(params.ValidationError, "guidance"):
            build(model="flux2-klein-9b", guidance=3)
        self.assertEqual(build(model="flux2-klein-9b").guidance, 1.0, "фиксированный guidance уходит в CLI")
        with self.assertRaisesRegex(params.ValidationError, "негативный"):
            build(model="z-image-turbo", negative_prompt="x")
        with self.assertRaisesRegex(params.ValidationError, "силы изменений"):
            build(model="z-image-turbo", strength=0.5)

    def test_bad_requests(self):
        for bad in (
            {"prompt": "  "}, {"prompt": "x" * 4001}, {"mode": "upscale"}, {"model": "sdxl"},
            {"style": "cubism"}, {"detail": "ultra"}, {"size": "100x100"}, {"seed": -2}, {"seed": 2**31},
        ):  # fmt: skip
            with self.assertRaises(params.ValidationError, msg=bad):
                build(**bad)
        with self.assertRaises(params.ValidationError):
            params.build_spec(["не объект"], AVAILABLE, SIZES.get)


class EditModesTest(unittest.TestCase):
    def test_img2img_takes_size_from_source(self):
        spec = build(mode="img2img", source=SRC, model="krea-2")
        self.assertEqual((spec.width, spec.height, spec.strength, spec.source), (1024, 768, 0.6, SRC))
        self.assertIsNone(build(mode="img2img", source=SRC, model="flux2-klein-9b").strength)
        with self.assertRaisesRegex(params.ValidationError, "исходное фото"):
            build(mode="img2img")
        with self.assertRaisesRegex(params.ValidationError, "не найдено"):
            build(mode="img2img", source="c" * 32)

    def test_default_model_follows_mode(self):
        self.assertEqual(build(mode="outpaint", source=SRC, padding={"left": 64}).model, "flux2-klein-9b")
        self.assertEqual(build(mode="inpaint", source=SRC, mask=MASK).model, "flux2-klein-9b", "для правки — Klein")
        self.assertEqual(build().model, "flux2-dev", "по промпту — первая в каталоге")

    def test_mode_the_model_cannot_do(self):
        with self.assertRaisesRegex(params.ValidationError, "Ideogram 4 не умеет режим «Поправить фото»"):
            build(mode="img2img", source=SRC, model="ideogram-4")
        with self.assertRaisesRegex(params.ValidationError, "не умеет режим «Расширить кадр»"):
            build(mode="outpaint", source=SRC, padding={"left": 64}, model="flux2-dev")
        with self.assertRaisesRegex(params.ValidationError, "не умеет режим «Удалить / заменить объект»"):
            build(mode="inpaint", source=SRC, mask=MASK, model="qwen-image-2512")

    def test_source_must_fit_the_grid(self):
        for size in ((1000, 768), (1024, 128), (4096, 1024)):
            with self.assertRaisesRegex(params.ValidationError, "кратны 32"):
                params.build_spec({"prompt": "p", "mode": "img2img", "source": SRC}, AVAILABLE, {SRC: size}.get)

    def test_inpaint_needs_matching_mask(self):
        spec = build(mode="inpaint", source=SRC, mask=MASK, model="flux2-klein-9b", strength=0.7)
        self.assertEqual((spec.mask, spec.strength, spec.width, spec.height), (MASK, 0.7, 1024, 768))
        self.assertEqual(build(mode="inpaint", source=SRC, mask=MASK).strength, 1.0)
        with self.assertRaisesRegex(params.ValidationError, "Выдели область"):
            build(mode="inpaint", source=SRC)
        with self.assertRaisesRegex(params.ValidationError, "не совпадает"):
            params.build_spec(
                {"prompt": "p", "mode": "inpaint", "source": SRC, "mask": MASK},
                AVAILABLE, {SRC: (1024, 768), MASK: (768, 768)}.get,
            )  # fmt: skip

    def test_outpaint_padding(self):
        spec = build(mode="outpaint", source=SRC, padding={"left": 128, "right": 64, "top": 32})
        self.assertEqual((spec.width, spec.height), (1024 + 192, 768 + 32))
        self.assertEqual(spec.padding, params.Padding(left=128, top=32, right=64, bottom=0))
        for bad in (None, {}, {"left": 0}, {"left": 50}, {"left": -32}, {"left": 1056}, {"top": "64"}):
            with self.assertRaises(params.ValidationError, msg=bad):
                build(mode="outpaint", source=SRC, padding=bad)


class CatalogTest(unittest.TestCase):
    def test_six_models_in_the_order_of_the_brief(self):
        self.assertEqual(
            [m.label for m in models.CATALOG],
            ["FLUX.2 [dev]", "Qwen-Image-2512", "FLUX.2 Klein 9B", "Z-Image Turbo", "Krea 2", "Ideogram 4"],
        )
        for mode in models.MODES:
            self.assertTrue(any(mode in m.modes for m in models.CATALOG), mode)

    def test_installed_by_marker_not_by_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "z-image-turbo-mflux-q8").mkdir()  # недокачанный каталог без отметки
            (root / models.FLUX2_DIR).mkdir()
            (root / models.FLUX2_DIR / f"{models.MARKER}-klein-9b").touch()
            found = {m.id: m.installed for m in models.resolve(models_dir=root, fake=False)}
            self.assertEqual([k for k, v in found.items() if v], ["flux2-klein-9b"])
            (root / "z-image-turbo-mflux-q8" / models.MARKER).touch()
            found = models.resolve(models_dir=root, fake=False)
            self.assertEqual(models.default(found).id, "flux2-klein-9b", "первая установленная по порядку каталога")
            self.assertEqual(models.default(found[3:]).id, "z-image-turbo")
            self.assertEqual(models.default(found, "inpaint").id, "flux2-klein-9b")


if __name__ == "__main__":
    unittest.main()
