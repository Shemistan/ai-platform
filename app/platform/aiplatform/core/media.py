"""ffmpeg и ffprobe: есть ли они и сколько длится файл."""

from __future__ import annotations

import functools
import shutil
import subprocess
from pathlib import Path


def ffmpeg_available(ffmpeg: str = "ffmpeg") -> bool:
    return shutil.which(ffmpeg) is not None


@functools.lru_cache(maxsize=8)
def has_filter(ffmpeg: str, name: str) -> bool:
    """Есть ли в сборке ffmpeg фильтр (drawtext — подписи и брендинг на кадрах). Кэш: баннер
    проблем собирается на каждый /api/config, а `-filters` — отдельный процесс."""
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(line.split()[1:2] == [name] for line in out.splitlines())


def ffprobe_for(ffmpeg: str = "ffmpeg") -> str:
    """ffprobe рядом с ffmpeg, иначе из PATH."""
    path = Path(ffmpeg)
    if path.parent != Path("."):
        candidate = path.with_name("ffprobe")
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def duration(path: Path, ffmpeg: str = "ffmpeg") -> float:
    """Длительность медиафайла в секундах; 0.0 — не узнать."""
    cmd = [ffprobe_for(ffmpeg), "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30).stdout
        return max(float(out.strip().splitlines()[0]), 0.0)
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return 0.0


def video_size(path: Path, ffmpeg: str = "ffmpeg") -> tuple[int, int]:
    """Ширина и высота кадра; (0, 0) — не узнать."""
    cmd = [ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "v:0"]
    cmd += ["-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30).stdout
        w, h = out.strip().split(",")[:2]
        return int(w), int(h)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0, 0
