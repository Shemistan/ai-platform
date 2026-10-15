"""Увеличение готовой картинки до 2K/4K: `mflux-upscale-seedvr2` (SeedVR2 3B).

SeedVR2 — диффузионный апскейлер за один шаг: дорисовывает мелкие детали, а не
растягивает пиксели. Размер задаётся короткой стороной; итог считаем так же,
как mflux 0.22.0 (`SeedVR2Util.preprocess_image`), чтобы показать его заранее.
Веса (~7 ГБ) mflux докачивает при первом запуске в HF_HOME — на диск моделей.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from aiplatform.photo.params import ValidationError

COMMAND = "mflux-upscale-seedvr2"
MODEL_ID, MODEL_LABEL = "seedvr2-3b", "SeedVR2 3B"
MODE, MODE_LABEL = "upscale", "Увеличение"

# Цель — длинная сторона результата.
TARGETS = {
    "2k": (2048, "2K"),
    "4k": (4096, "4K"),
}


@dataclass(frozen=True)
class UpscaleSpec:
    parent: str  # id задачи с исходной картинкой
    target: str
    resolution: int  # короткая сторона результата — то, что принимает --resolution
    width: int
    height: int
    seed: int


def output_size(width: int, height: int, resolution: int) -> tuple[int, int]:
    """Размер, который выдаст SeedVR2 для короткой стороны resolution."""
    scale = resolution / min(width, height)
    return int(width * scale) // 2 * 2, int(height * scale) // 2 * 2


def short_side(width: int, height: int, long_side: int) -> int:
    """Короткая сторона, при которой длинная станет long_side. Вверх: SeedVR2 отбрасывает дробь и нечётный пиксель."""
    return math.ceil(min(width, height) * long_side / max(width, height))


def targets_for(width: int, height: int) -> list[str]:
    """Цели, которые действительно увеличивают картинку."""
    return [key for key, (side, _) in TARGETS.items() if side > max(width, height)]


def build_spec(payload: object, parent: str, width: int, height: int, seed: int) -> UpscaleSpec:
    target = payload.get("target") if isinstance(payload, dict) else None
    if target not in TARGETS:
        raise ValidationError(f"Увеличение: выбери {' или '.join(label for _, label in TARGETS.values())}.")
    if target not in targets_for(width, height):
        raise ValidationError(f"Картинка {width}×{height} уже не меньше {TARGETS[target][1]}.")
    resolution = short_side(width, height, TARGETS[target][0])
    out_w, out_h = output_size(width, height, resolution)
    return UpscaleSpec(parent=parent, target=target, resolution=resolution, width=out_w, height=out_h, seed=seed)


def build_argv(
    cmd: tuple[str, ...], spec: UpscaleSpec, source: Path, output: Path, *, low_ram: bool = False
) -> list[str]:
    argv = [
        *cmd,
        "--model", MODEL_ID,
        "--image-path", str(source),
        "--resolution", str(spec.resolution),
        "--seed", str(spec.seed),
        "--vae-tiling",  # VAE по тайлам: 4K целиком требует слишком много памяти
        "--output", str(output),
    ]  # fmt: skip
    if low_ram:
        argv.append("--low-ram")
    return argv
