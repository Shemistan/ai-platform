"""Имитация `mflux-generate-*` и `Flux2CLI` для демо-режима и тестов.

Первый аргумент — имя настоящей команды (`mflux-generate-qwen`, `flux2`),
дальше её настоящие флаги. Вывод в том же формате, что у настоящих CLI,
поэтому сервис идёт по тому же пути: подпроцесс → вывод → разбор → статус.
Картинка — цветной градиент нужного размера; в режимах правки на него
накладывается рамка, показывающая, где был исходник.

Окружение: FAKE_GEN_STEP_DELAY — пауза на шаг, с (по умолчанию 0.4);
FAKE_GEN_FAIL=1 — упасть посреди шагов.
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import time
import zlib


def _mflux_parser(command: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=command)
    parser.add_argument("--model", required=True)
    parser.add_argument("--base-model")
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--negative-prompt", default="")
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--steps", type=int)
    parser.add_argument("--preset")
    parser.add_argument("--guidance", type=float)
    parser.add_argument("--image", nargs="+")
    parser.add_argument("--low-ram", action="store_true")
    parser.add_argument("--output", required=True)
    return parser


def _flux2_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="flux2")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("t2i", "i2i"):
        cmd = sub.add_parser(name)
        cmd.add_argument("prompt")
        cmd.add_argument("--model", required=True)
        cmd.add_argument("--width", type=int, required=True)
        cmd.add_argument("--height", type=int, required=True)
        cmd.add_argument("--memory-profile")
        if name == "i2i":
            cmd.add_argument("--images", required=True)
    for name in ("inpaint", "outpaint"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--flux-model", required=True)
        cmd.add_argument("--image", required=True)
        cmd.add_argument("--prompt", required=True)
        if name == "inpaint":
            cmd.add_argument("--mask", required=True)
            cmd.add_argument("--strength", type=float, required=True)
            cmd.add_argument("--max-pixels", type=int)
            cmd.add_argument("--composite-on-original", action="store_true")
        else:
            for side in ("left", "top", "right", "bottom"):
                cmd.add_argument(f"--{side}", type=int, default=0)
    for cmd in sub.choices.values():
        cmd.add_argument("--steps", type=int, required=True)
        cmd.add_argument("--guidance", type=float, required=True)
        cmd.add_argument("--seed", type=int, required=True)
        cmd.add_argument("--output", required=True)
        cmd.add_argument("--models-dir", required=True)
        if cmd.prog.split()[-1] != "outpaint":  # у настоящего outpaint флагов квантизации нет
            cmd.add_argument("--text-quant", required=True)
            cmd.add_argument("--transformer-quant", required=True)
    return parser


def _say(text: str, end: str = "\n") -> None:
    sys.stderr.write(text + end)
    sys.stderr.flush()


def png_size(path: str) -> tuple[int, int]:
    with open(path, "rb") as fh:
        head = fh.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n" or len(head) < 24:
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", head[16:24])


def write_png(path: str, width: int, height: int, seed: int, inset: tuple[int, int, int, int] | None = None) -> None:
    """Градиент, зависящий от seed. inset (x0, y0, x1, y1) — область исходника, она затемнена."""
    r0, g0, b0 = (seed * 53) % 256, (seed * 97 + 80) % 256, (seed * 31 + 160) % 256
    # Красный и синий зависят только от x, зелёный — только от y: строка собирается срезами, без цикла по пикселям.
    base = bytearray(3 * width)
    for x in range(width):
        gx = 255 * x // max(width - 1, 1)
        base[3 * x], base[3 * x + 2] = (r0 + gx) % 256, (b0 + gx // 2) % 256
    darker = bytes(c // 3 for c in range(256))
    rows = bytearray()
    for y in range(height):
        row = bytearray(base)
        row[1::3] = bytes([(g0 + 255 * y // max(height - 1, 1)) % 256]) * width
        if inset and inset[1] <= y < inset[3]:
            x0, x1 = 3 * inset[0], 3 * inset[2]
            row[x0:x1] = row[x0:x1].translate(darker)
        rows.append(0)  # фильтр строки: None
        rows += row

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n")
        fh.write(chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)))
        fh.write(chunk(b"IDAT", zlib.compress(bytes(rows), 1)))
        fh.write(chunk(b"IEND", b""))


def _fail_requested(n: int, steps: int) -> bool:
    return bool(os.environ.get("FAKE_GEN_FAIL")) and n == max(steps // 2, 1)


def _run_mflux(command: str, argv: list[str], delay: float) -> int:
    args = _mflux_parser(command).parse_args(argv)
    steps = args.steps or int(args.preset.rsplit("_", 1)[1])
    first = 0
    if args.image:
        if not os.path.isfile(args.image[0]):
            _say(f"error: image not found: {args.image[0]}")
            return 2
        first = max(1, int(steps * float(args.image[1])))  # как у mflux: часть шагов пропускается
    total = max(steps - first, 1)
    time.sleep(delay)
    for n in range(total + 1):
        bar = ("█" * round(10 * n / total)).ljust(10)
        _say(f"\r{round(100 * n / total):3d}%|{bar}| {n}/{total} [00:0{n % 10}<00:00, {delay:.2f}s/it]", end="")
        if _fail_requested(n, total):
            _say("\nRuntimeError: fake failure requested by FAKE_GEN_FAIL")
            return 1
        if n < total:
            time.sleep(delay)
    _say("")
    write_png(args.output, args.width, args.height, args.seed)
    return 0


def _run_flux2(argv: list[str], delay: float) -> int:
    args = _flux2_parser().parse_args(argv)
    source = getattr(args, "images", None) or getattr(args, "image", None)
    inset = None
    if args.command in ("t2i", "i2i"):
        width, height = args.width, args.height
    else:
        try:
            width, height = png_size(source)
            if args.command == "inpaint" and png_size(args.mask) != (width, height):
                print("Error: mask dimensions do not match the image")
                return 64
        except (OSError, ValueError):
            print(f"Error: Could not decode image at {source}")
            return 64
        if args.command == "outpaint":
            inset = (args.left, args.top, args.left + width, args.top + height)
            width += args.left + args.right
            height += args.top + args.bottom
    print(f"Generating {width}x{height}, {args.steps} steps, guidance {args.guidance}, seed {args.seed}...")
    print("Loading text encoder...", flush=True)
    time.sleep(delay)
    print("Loading transformer...", flush=True)
    time.sleep(delay)
    for n in range(1, args.steps + 1):
        if _fail_requested(n, args.steps):
            print("Error: fake failure requested by FAKE_GEN_FAIL")
            return 1
        time.sleep(delay)
        print(f"Step {n}/{args.steps} {int(delay * 1000)}ms", flush=True)
    print("Denoising complete, decoding image...", flush=True)
    write_png(args.output, width, height, args.seed, inset)
    print(f"Output saved to: {args.output}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        _say("usage: fake_gen.py <command> [flags]")
        return 2
    delay = float(os.environ.get("FAKE_GEN_STEP_DELAY", "0.4"))
    command, rest = argv[0], argv[1:]
    if command == "flux2":
        return _run_flux2(rest, delay)
    return _run_mflux(command, rest, delay)


if __name__ == "__main__":
    sys.exit(main())
