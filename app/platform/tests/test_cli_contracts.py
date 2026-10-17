"""Флаги build_argv есть в справке настоящих CLI (QA-005): снапшоты tests/contracts/.

Снапшоты снимает `make contracts` с установленных пайплайнов; сам тест моделей не требует.
Флаг, которого нет в снапшоте, — пайплайн его переименовал или убрал: поправь build_argv
или пересними снапшоты после обновления пайплайна.
"""

import unittest
from pathlib import Path

from aiplatform import contracts
from aiplatform.photo import flux2, mflux, models, params, upscale
from aiplatform.photo.params import ValidationError
from aiplatform.video import ltx, wan
from aiplatform.video import models as video_models
from aiplatform.video.timeline import build_spec

AVAILABLE = models.resolve(models_dir=Path("/models"), fake=True)
SRC, MASK = "a" * 32, "b" * 32
OUT, SRC_PATH, MASK_PATH = Path("/jobs/x/output.png"), Path("/up/src.png"), Path("/up/mask.png")
MODE_PAYLOAD = {
    models.TXT2IMG: {},
    models.IMG2IMG: {"source": SRC},
    models.INPAINT: {"source": SRC, "mask": MASK},
    models.OUTPAINT: {"source": SRC, "padding": {"left": 128, "bottom": 64}},
}
# Все ручки сразу: флаги, которые уходят не всегда (guidance, negative, пресет качества).
EXTRAS = ({}, {"guidance": 5, "negative_prompt": "-blur", "detail": "high"})


def photo_spec(model, mode, extra):
    sizes = {SRC: (1024, 768), MASK: (1024, 768)}
    payload = {"prompt": "-кот", "seed": 7, "model": model.id, "mode": mode, **MODE_PAYLOAD[mode], **extra}
    if mode in model.strength_modes:
        payload["strength"] = 0.7
    return params.build_spec(payload, AVAILABLE, sizes.get)


def photo_cases():
    """(снапшот, argv) для каждой модели «Фото», режима и набора ручек, с low_ram и без."""
    for available in AVAILABLE:
        model = available.model
        for mode in model.modes:
            for extra in EXTRAS:
                try:
                    spec = photo_spec(model, mode, extra)
                except ValidationError:
                    continue  # ручка этой моделью не принимается — такого argv в бою не будет
                for low_ram in (False, True):
                    if model.backend == models.FLUX2:
                        argv = flux2.build_argv(
                            ("Flux2CLI",), model, spec, OUT,
                            models_dir=available.path, source=SRC_PATH, mask=MASK_PATH, low_ram=low_ram,
                        )  # fmt: skip
                        yield f"flux2-{argv[1]}", argv[2:], f"{model.id} {mode} {extra}"
                    else:
                        argv = mflux.build_argv(
                            (model.command,), model, spec, OUT, weights=available.path, source=SRC_PATH, low_ram=low_ram
                        )
                        yield model.command, argv[1:], f"{model.id} {mode} {extra}"


def video_cases():
    for model in video_models.CATALOG:
        if model.pipeline == "wan":
            spec = build_spec({"prompt": "-p", "duration_s": 5, "seed": 7}, model.limits)
            yield (
                "pocketai-wan-video",
                wan.build_argv(("wan",), spec, Path("o.mp4"), model="/m", steps=10)[1:],
                model.id,
            )
            continue
        for audio in (True, False):
            spec = build_spec({"prompt": "-p", "audio": audio, "frames": [{"upload_id": "a"}]}, model.limits)
            for low_ram in (False, True):
                argv = ltx.build_argv(
                    ("ltx",), spec, [Path("a.png")], Path("o.mp4"), model="m", gemma="/g", low_ram=low_ram
                )
                yield f"ltx-2-mlx-{argv[1]}", argv[2:], f"{model.id} audio={audio}"


def upscale_cases():
    spec = upscale.build_spec({"target": "2k"}, "p", 1344, 768, 42)
    for low_ram in (False, True):
        argv = upscale.build_argv((upscale.COMMAND,), spec, Path("/in.png"), Path("/out.png"), low_ram=low_ram)
        yield upscale.COMMAND, argv[1:], "seedvr2"


class CliContractTest(unittest.TestCase):
    def check(self, cases):
        seen = 0
        for name, argv, what in cases:
            path = contracts.snapshot_path(name)
            self.assertTrue(path.is_file(), f"нет снапшота {path.name}: сними его `make contracts C={name}`")
            known = contracts.flags(path.read_text(encoding="utf-8"))
            for flag in contracts.argv_flags(argv):
                seen += 1
                self.assertIn(
                    flag,
                    known,
                    f"{name} не знает флаг {flag} ({what}): пайплайн его переименовал или убрал — "
                    f"поправь build_argv или пересними снапшот `make contracts C={name}`",
                )
        self.assertGreater(seen, 0, "argv не собрался ни разу")

    def test_photo_pipelines(self):
        self.check(photo_cases())

    def test_video_pipelines(self):
        self.check(video_cases())

    def test_upscale(self):
        self.check(upscale_cases())

    def test_every_snapshot_is_used_and_targets_match(self):
        """Снапшоты ровно для тех CLI, что зовёт платформа (имена — из contracts.targets)."""
        names = set(contracts.targets({"AIP_DATA_DIR": "/nonexistent-data", "AIP_TEST_GUARD": ""}))
        used = {n for n, _, _ in (*photo_cases(), *video_cases(), *upscale_cases())}
        self.assertEqual(used, names)
        on_disk = {p.name.removesuffix("-help.txt") for p in contracts.SNAPSHOTS.glob("*-help.txt")}
        self.assertEqual(on_disk, names)


class FlagParsingTest(unittest.TestCase):
    def test_flags_from_help_and_argv(self):
        text = "usage: x [--model MODEL] [--no-audio]\n  --text-quant <q>   (default: 4bit)\n  -h, --help"
        self.assertEqual(contracts.flags(text), {"--model", "--no-audio", "--text-quant", "--help"})
        argv = ["--model", "m", "--prompt=-p --fake", "--seed", "7", "--", "--positional"]
        self.assertEqual(contracts.argv_flags(argv), ["--model", "--prompt", "--seed"])

    def test_renamed_flag_fails_with_hint(self):
        case = CliContractTest("test_upscale")
        renamed = [("mflux-upscale-seedvr2", ["--image-file", "x"], "seedvr2")]
        with self.assertRaisesRegex(AssertionError, r"не знает флаг --image-file .*make contracts"):
            case.check(renamed)


if __name__ == "__main__":
    unittest.main()
