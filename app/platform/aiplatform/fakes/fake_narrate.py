"""Имитация `narrate say` для демо-режима и тестов.

Принимает те же флаги, печатает те же строки, что narrator вне терминала
(«абзац n/N», «[i/N] … с аудио …»), и пишет mp3 длиной по числу слов
(~150 слов в минуту): тон 220 Гц через ffmpeg. Без ffmpeg — WAV-заглушка.

Окружение: FAKE_TTS_STEP_DELAY — пауза на абзац, с (по умолчанию 0.3);
FAKE_TTS_FAIL=1 — упасть на середине.
"""

from __future__ import annotations

import argparse
import os
import shutil
import struct
import subprocess
import sys
import time

WORDS_PER_SECOND = 2.5
MIN_SECONDS = 1.0


def paragraphs(text: str) -> list[str]:
    out, buf = [], []
    for line in text.splitlines() + [""]:
        if line.strip():
            buf.append(line.strip())
        elif buf:
            out.append(" ".join(buf))
            buf = []
    return out


def seconds_for(text: str) -> float:
    return max(len(text.split()) / WORDS_PER_SECOND, MIN_SECONDS)


def write_wav(path: str, seconds: float, rate: int = 8000) -> None:
    frames = int(seconds * rate)
    data = b"\x00\x00" * frames
    header = b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt "
    header += struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16) + b"data" + struct.pack("<I", len(data))
    with open(path, "wb") as fh:
        fh.write(header + data)


def write_mp3(path: str, seconds: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        write_wav(path, seconds)
        return
    cmd = [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds:.2f}"]
    cmd += ["-ac", "1", "-c:a", "libmp3lame", "-b:a", "64k", path]
    subprocess.run(cmd, check=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-narrate")
    sub = parser.add_subparsers(dest="cmd", required=True)
    say = sub.add_parser("say")
    say.add_argument("file")
    say.add_argument("--engine", required=True)
    say.add_argument("--voice")
    say.add_argument("--speed", type=float)
    say.add_argument("--steps", type=int)
    say.add_argument("--out", required=True)
    say.add_argument("--no-verify", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    with open(args.file, encoding="utf-8") as fh:
        text = fh.read()
    blocks = paragraphs(text)
    if not blocks:
        print(f"{args.file}: текст для озвучки пуст", file=sys.stderr)
        return 2
    delay = float(os.environ.get("FAKE_TTS_STEP_DELAY", "0.3"))
    title = os.path.basename(args.file)
    total = 0.0
    for n, block in enumerate(blocks, start=1):
        print(f"{title} абзац {n}/{len(blocks)}", file=sys.stderr, flush=True)
        if os.environ.get("FAKE_TTS_FAIL") and n == max(len(blocks) // 2, 1):
            print("RuntimeError: fake failure requested by FAKE_TTS_FAIL", file=sys.stderr, flush=True)
            return 1
        time.sleep(delay)
        seconds = seconds_for(block) / (args.speed or 1.0)
        total += seconds
        print(
            f"[{n}/{len(blocks)}] {seconds:5.1f} с аудио за {delay:4.0f} с  {block[:50]}", file=sys.stderr, flush=True
        )
    print(f"синтез: {delay * len(blocks):.0f} с, склеиваю {len(blocks)} клипов", file=sys.stderr, flush=True)
    write_mp3(args.out, total)
    print(f"{args.out}  {total:.0f} с")
    return 0


if __name__ == "__main__":
    sys.exit(main())
