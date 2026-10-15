"""Имитация `pocketai-wan-video` для демо-режима и тестов.

Принимает те же флаги и печатает вывод в том же формате, что настоящий CLI:
этапы с цветовыми кодами терминала, полоса tqdm `Diffusion` через `\\r`.
Скорость и сбой задаются теми же переменными, что у fake_ltx.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

try:
    from aiplatform.fakes import fake_ltx
except ImportError:  # запущен файлом, как и fake_ltx: пакета в путях нет, сосед — есть
    import fake_ltx

FPS = 16
_BLUE, _GREEN, _RESET = "\033[94m", "\033[92m", "\033[0m"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fake-wan")
    parser.add_argument("--model-dir", required=True)
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--output-path", required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--num-frames", type=int, required=True)
    parser.add_argument("--steps", type=int, required=True)
    parser.add_argument("--scheduler", choices=["euler", "dpm++", "unipc"], default="unipc")
    parser.add_argument("--seed", type=int, default=-1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    delay = float(os.environ.get("FAKE_LTX_STEP_DELAY", "0.6"))
    if (args.num_frames - 1) % 4:
        fake_ltx._err(f"AssertionError: num_frames must be 4n+1, got {args.num_frames}")
        return 1

    print(f"  Size: {args.width}x{args.height}, Frames: {args.num_frames} (fake, {args.model_dir})")
    for label in ("Loading T5 encoder...", "Encoding text...", "Loading transformer model(s)..."):
        print(f"\n{_BLUE}{label}{_RESET}", flush=True)
        time.sleep(delay)
    print(f"\n{_GREEN}Denoising ({args.steps} steps)...{_RESET}", flush=True)
    for n in range(args.steps + 1):
        pct = round(100 * n / args.steps)
        bar = ("█" * round(10 * n / args.steps)).ljust(10)
        fake_ltx._err(f"\rDiffusion: {pct:3d}%|{bar}| {n}/{args.steps} [00:0{n}<00:00, {delay:.2f}s/it]", end="")
        if n == 2 and os.environ.get("FAKE_LTX_FAIL"):
            fake_ltx._err("\nRuntimeError: fake failure requested by FAKE_LTX_FAIL")
            return 1
        if n < args.steps:
            time.sleep(delay)
    fake_ltx._err("")
    print(f"\n{_BLUE}Decoding with VAE...{_RESET}", flush=True)
    time.sleep(delay)
    fake_ltx._render(
        argparse.Namespace(
            output=args.output_path,
            frames=args.num_frames,
            frame_rate=FPS,
            width=args.width,
            height=args.height,
            image=[],
        )
    )
    print(f"\n{_GREEN}✓ Video saved to {args.output_path}{_RESET}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
