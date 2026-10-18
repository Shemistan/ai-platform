"""Имитация SadTalker `inference.py` для демо-режима и тестов.

Принимает те же флаги и печатает вывод в том же формате (полосы tqdm через
`\\r`, итоговая строка с путём), поэтому сервис идёт по тому же пути. Видео —
картинка персонажа под звуком речи, собранная ffmpeg.

Окружение: FAKE_SADTALKER_STEP_DELAY — пауза на шаг, с (по умолчанию 0.3);
FAKE_SADTALKER_NO_FACE=1 — «лица нет»: как настоящий, выйти с кодом 0 без видео;
FAKE_SADTALKER_FAIL=1 — упасть на анимации.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fake-sadtalker")
    parser.add_argument("--driven_audio", required=True)
    parser.add_argument("--source_image", required=True)
    parser.add_argument("--result_dir", required=True)
    parser.add_argument("--checkpoint_dir", required=True)
    parser.add_argument("--size", type=int, choices=(256, 512), default=256)
    parser.add_argument("--preprocess", choices=("crop", "extcrop", "resize", "full", "extfull"), default="crop")
    parser.add_argument("--still", action="store_true")
    parser.add_argument("--enhancer", choices=("gfpgan", "RestoreFormer"))
    parser.add_argument("--cpu", action="store_true")
    return parser


def _err(text: str, end: str = "\n") -> None:
    sys.stderr.write(text + end)
    sys.stderr.flush()


def _bar(desc: str, steps: int, delay: float) -> None:
    for n in range(steps + 1):
        pct = round(100 * n / steps)
        bar = ("█" * round(10 * n / steps)).ljust(10)
        _err(f"\r{desc}: {pct:3d}%|{bar}| {n}/{steps} [00:0{n}<00:0{steps - n}, {delay:.2f}s/it]", end="")
        if n < steps:
            time.sleep(delay)
    _err("")


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    delay = float(os.environ.get("FAKE_SADTALKER_STEP_DELAY", "0.3"))
    for path in (args.driven_audio, args.source_image):
        if not os.path.isfile(path):
            _err(f"FileNotFoundError: {path}")
            return 1
    stamp = time.strftime("%Y_%m_%d_%H.%M.%S")
    save_dir = os.path.join(args.result_dir, stamp)
    os.makedirs(save_dir, exist_ok=True)
    print("using safetensor as default", flush=True)
    print("3DMM Extraction for source image", flush=True)
    _bar("landmark Det:", 1, delay)
    _bar("3DMM Extraction In Video:", 1, delay)
    if os.environ.get("FAKE_SADTALKER_NO_FACE"):
        print("Can't get the coeffs of the input", flush=True)
        return 0
    _bar("mel:", 3, delay)
    _bar("audio2exp:", 3, delay)
    _bar("Face Renderer:", 5, delay)
    if os.environ.get("FAKE_SADTALKER_FAIL"):
        _err("RuntimeError: fake failure requested by FAKE_SADTALKER_FAIL")
        return 1
    if args.preprocess == "full":
        _bar("seamlessClone:", 3, delay)
    if args.enhancer:
        _bar("Face Enhancer:", 3, delay)
    output = save_dir + ".mp4"
    size = f"{args.size}:{args.size}"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", args.source_image, "-i", args.driven_audio]
    cmd += ["-vf", f"scale={size}:force_original_aspect_ratio=increase,crop={size},setsar=1,fps=25"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", output]
    subprocess.run(cmd, check=True)
    print(f"The generated video is named: {output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
