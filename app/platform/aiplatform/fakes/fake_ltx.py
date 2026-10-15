"""Имитация `ltx-2-mlx generate` для демо-режима и тестов.

Принимает те же флаги и печатает вывод в том же формате, что настоящий CLI
(фазы в stderr, полосы tqdm через `\\r`), поэтому сервис идёт по тому же пути.
Видео — слайд-шоу из приложенных кадров (или цветной фон), собранное ffmpeg.

Окружение: FAKE_LTX_STEP_DELAY — пауза на шаг, с (по умолчанию 0.6);
FAKE_LTX_FAIL=1 — упасть на втором проходе; FAKE_LTX_NO_FFMPEG=1 — записать
заглушку вместо mp4 (для тестов без ffmpeg).
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fake-ltx")
    sub = parser.add_subparsers(dest="command", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--distilled", action="store_true", required=True)
    gen.add_argument("--model", "-m", required=True)
    gen.add_argument("--gemma")
    gen.add_argument("--prompt", "-p", required=True)
    gen.add_argument("--output", "-o", required=True)
    gen.add_argument("--height", "-H", type=int, required=True)
    gen.add_argument("--width", "-W", type=int, required=True)
    gen.add_argument("--frames", "-f", type=int, required=True)
    gen.add_argument("--frame-rate", type=float, required=True)
    gen.add_argument("--seed", "-s", type=int, default=-1)
    gen.add_argument("--no-audio", action="store_true")
    gen.add_argument("--low-ram", action="store_true")
    gen.add_argument("--image", "-i", action="append", nargs=3, default=[], metavar=("PATH", "FRAME", "STRENGTH"))
    return parser


def _err(text: str, end: str = "\n") -> None:
    sys.stderr.write(text + end)
    sys.stderr.flush()


def _phase(label: str, delay: float) -> None:
    _err(f"[{label}] ...")
    time.sleep(delay)
    _err(f"[{label}] done in {delay:.1f}s")


def _denoise(steps: int, delay: float) -> None:
    for n in range(steps + 1):
        pct = round(100 * n / steps)
        bar = ("█" * round(10 * n / steps)).ljust(10)
        _err(f"\rDenoising: {pct:3d}%|{bar}| {n}/{steps} [00:0{n}<00:0{steps - n}, {delay:.2f}s/it]", end="")
        if n < steps:
            time.sleep(delay)
    _err("")


def _render(args: argparse.Namespace) -> None:
    if os.environ.get("FAKE_LTX_NO_FFMPEG") or not shutil.which("ffmpeg"):
        with open(args.output, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + b"fake-ltx placeholder " * 200)
        return
    duration = args.frames / args.frame_rate
    size = f"{args.width}x{args.height}"
    cmd = ["ffmpeg", "-y", "-loglevel", "error"]
    if args.image:
        per = duration / len(args.image)
        for path, _, _ in args.image:
            cmd += ["-loop", "1", "-t", f"{per:.3f}", "-i", path]
        scale = (
            f"scale={args.width}:{args.height}:force_original_aspect_ratio=increase,"
            f"crop={args.width}:{args.height},setsar=1,fps={args.frame_rate}"
        )
        chains = "".join(f"[{i}:v]{scale}[v{i}];" for i in range(len(args.image)))
        inputs = "".join(f"[v{i}]" for i in range(len(args.image)))
        cmd += ["-filter_complex", f"{chains}{inputs}concat=n={len(args.image)}:v=1:a=0[out]", "-map", "[out]"]
    else:
        cmd += ["-f", "lavfi", "-i", f"testsrc2=size={size}:rate={args.frame_rate}:duration={duration:.3f}"]
    cmd += ["-pix_fmt", "yuv420p", "-movflags", "+faststart", args.output]
    subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    delay = float(os.environ.get("FAKE_LTX_STEP_DELAY", "0.6"))
    for path, frame, _ in args.image:
        if not os.path.isfile(path):
            _err(f"error: image not found: {path}")
            return 2
        if not 0 <= int(frame) < args.frames:
            _err(f"error: --image {path}: frame index {frame} is past the last of {args.frames} frames")
            return 2

    print("Mode: Distilled Two-Stage (half-res + upscale + distilled refine)")
    print(f"  Model: {args.model} (fake)", flush=True)
    _phase("Loading text encoder (Gemma)", delay)
    _phase("Encoding prompt", delay)
    _phase("Loading transformer (transformer-distilled.safetensors)", delay)
    _denoise(8, delay)
    if os.environ.get("FAKE_LTX_FAIL"):
        _err("RuntimeError: fake failure requested by FAKE_LTX_FAIL")
        return 1
    _denoise(3, delay)
    _phase(
        "Loading decoders (VAE only, --no-audio)" if args.no_audio else "Loading decoders (VAE + audio + vocoder)",
        delay,
    )
    _render(args)
    print(f"Saved: {args.output}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
