"""Имитация HyperFrames CLI (`hyperframes render`) для демо и тестов.

Читает композицию из рабочего каталога, берёт длительность (`data-duration` корня) и текст
первой карточки и рисует ffmpeg прозрачный MOV (ProRes 4444): полупрозрачная плашка с
текстом внизу слева. Окружение: FAKE_HYPERFRAMES_FAIL=1 — упасть.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from aiplatform import config as platform_config  # noqa: E402 — путь пакета добавлен строкой выше
from aiplatform.montage import motion  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command")
    parser.add_argument("--composition", default="index.html")
    parser.add_argument("--format", default="mp4")
    parser.add_argument("--fps", default="30")
    parser.add_argument("--output", "-o", required=True)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    if args.command != "render" or args.format != "mov":
        print(f"fake hyperframes: умею только render --format mov, а не {args.command} {args.format}", file=sys.stderr)
        return 2
    if os.environ.get("FAKE_HYPERFRAMES_FAIL"):
        print("fake hyperframes: браузер не запустился", file=sys.stderr)
        return 1
    document = Path(args.composition).read_text(encoding="utf-8")
    found = re.search(r'id="stage"[^>]*data-duration="([\d.]+)"', document)
    duration = float(found[1]) if found else 4.0
    size = re.search(r'id="stage"[^>]*data-width="(\d+)" data-height="(\d+)"', document)
    width, height = (int(size[1]), int(size[2])) if size else (1920, 1080)
    text_file = Path(args.output).with_suffix(".txt")
    text_file.write_text(motion.first_text(document) or "вставка", encoding="utf-8")
    font = next((f for f in platform_config.FONT_CANDIDATES if Path(f).is_file()), "")
    draw = f"drawtext=textfile='{text_file}':fontcolor=white:fontsize=56:x=140:y=h-300"
    if font:
        draw += f":fontfile='{font}'"
    graph = f"color=c=black@0.0:s={width}x{height}:r={args.fps}:d={duration},format=yuva444p10le," \
            f"drawbox=x=96:y=h-360:w=1100:h=150:color=0x1c1e21@0.85:t=fill,{draw}"  # fmt: skip
    argv_ = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", graph, "-c:v", "prores_ks",
             "-profile:v", "4444", "-pix_fmt", "yuva444p10le", args.output]  # fmt: skip
    proc = subprocess.run(argv_, capture_output=True, text=True)
    text_file.unlink(missing_ok=True)
    if proc.returncode:
        print(proc.stderr, file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
