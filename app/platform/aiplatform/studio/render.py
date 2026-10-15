"""Сборка видео ffmpeg'ом: постер, «живые» слайды под озвучку, склейка со звуком.

Слайд — видео-фон (ролик из внешней модели: по кругу, без своего звука) или
картинка-фон с медленным наездом (zoompan), затемнение снизу,
заголовок и тезисы, которые появляются по очереди (drawtext с alpha по
времени). Без картинки — тёмный фон с лёгким градиентом. Тексты идут
через textfile с expansion=none: ни кавычки, ни двоеточия, ни % в тексте
не ломают фильтр. Горизонталь 1920×1080 и вертикаль 1080×1920 — одна сцена,
разная раскладка.
"""

from __future__ import annotations

import subprocess
import textwrap
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aiplatform.core import runner
from aiplatform.studio.llm import StudioError

LANDSCAPE, VERTICAL = "landscape", "vertical"
SIZES = {LANDSCAPE: (1920, 1080), VERTICAL: (1080, 1920)}
FPS = 25
ZOOM_PER_FRAME = 0.0007
ZOOM_MAX = 1.18
BACKGROUND = "0x141a26"
ACCENT = "0x7ea2ff"
INTRO_S = 2.0
FFMPEG_HINT = "Нужен ffmpeg: brew install ffmpeg (или путь в AIP_FFMPEG)."


@dataclass(frozen=True)
class Layout:
    width: int
    height: int
    title_size: int
    bullet_size: int
    margin: int
    title_y: int  # верх заголовка
    shade_y: int  # откуда начинается затемнение
    title_chars: int  # сколько знаков в строке заголовка
    bullet_chars: int


def layout(orientation: str, scale: float = 1.0) -> Layout:
    w, h = SIZES[orientation]
    w, h = _even(w * scale), _even(h * scale)
    if orientation == VERTICAL:
        title, bullet, margin = round(h * 0.036), round(h * 0.024), round(w * 0.07)
        top = round(h * 0.58)
        return Layout(w, h, title, bullet, margin, top, round(h * 0.38), 24, 34)
    title, bullet, margin = round(h * 0.062), round(h * 0.04), round(w * 0.05)
    top = round(h * 0.6)
    return Layout(w, h, title, bullet, margin, top, round(h * 0.36), 44, 70)


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def wrap(text: str, chars: int, lines: int) -> str:
    out = textwrap.wrap(" ".join(text.split()), width=chars) or [""]
    if len(out) > lines:
        out = out[:lines]
        out[-1] = out[-1].rstrip(" .,;:") + "…"
    return "\n".join(out)


def _quote(path: Path | str) -> str:
    """Путь в аргументе фильтра ffmpeg: в одинарных кавычках, кавычки и \\ экранированы."""
    return "'" + str(path).replace("\\", "\\\\").replace("'", r"'\''") + "'"


def _background(image: Path | None, lay: Layout, frames: int, clip: Path | None = None) -> tuple[list[str], str]:
    """Вход и цепочка фильтров фона: видео (по кругу, без звука), картинка с наездом или градиент."""
    w, h = lay.width, lay.height
    if clip is not None:
        args = ["-stream_loop", "-1", "-i", str(clip)]
        chain = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS},setsar=1"
        return args, chain
    if image is not None:
        args = ["-loop", "1", "-framerate", str(FPS), "-i", str(image)]
        chain = (
            f"[0:v]scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},"
            f"zoompan=z='min(zoom+{ZOOM_PER_FRAME},{ZOOM_MAX})':d={frames}"
            f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={FPS},setsar=1"
        )
        return args, chain
    args = ["-f", "lavfi", "-i", f"color=c={BACKGROUND}:s={w}x{h}:r={FPS}"]
    chain = f"[0:v]drawbox=x=0:y=0:w=iw*0.012:h=ih:color={ACCENT}@0.9:t=fill,setsar=1"
    return args, chain


def slide_filter(lay: Layout, font: str, title_file: Path, bullet_files: list[Path], seconds: float) -> str:
    """Тексты поверх фона: заголовок проявляется первым, тезисы — по очереди за ним."""
    # Затемнение снизу ступенями — мягкий переход вместо резкой плашки.
    band = max((lay.title_y - lay.shade_y) // 4, 1)
    parts = [
        f"drawbox=x=0:y={lay.shade_y + n * band}:w=iw:h={band}:color=black@{0.12 * (n + 1):.2f}:t=fill"
        for n in range(4)
    ]
    parts.append(f"drawbox=x=0:y={lay.shade_y + 4 * band}:w=iw:h=ih-{lay.shade_y + 4 * band}:color=black@0.6:t=fill")
    accent_h = max(lay.title_size // 10, 2)
    parts.append(
        f"drawbox=x={lay.margin}:y={lay.title_y - 3 * accent_h}:w={lay.title_size * 2}:h={accent_h}"
        f":color={ACCENT}@0.95:t=fill"
    )
    font_arg = f"fontfile={_quote(font)}:" if font else ""
    title_lines = max(1, title_file.read_text(encoding="utf-8").count("\n") + 1)
    y = lay.title_y
    parts.append(
        f"drawtext={font_arg}textfile={_quote(title_file)}:expansion=none:fontsize={lay.title_size}"
        f":fontcolor=white:line_spacing={lay.title_size // 4}:x={lay.margin}:y={y}"
        f":alpha='min(1,max(0,(t-0.2)/0.5))'"
    )
    y += title_lines * round(lay.title_size * 1.3) + lay.bullet_size
    step = min(0.7, max(seconds - 1.2, 0.3) / max(len(bullet_files), 1))
    for n, path in enumerate(bullet_files):
        appear = 0.8 + n * step
        lines = path.read_text(encoding="utf-8").count("\n") + 1
        parts.append(
            f"drawtext={font_arg}textfile={_quote(path)}:expansion=none:fontsize={lay.bullet_size}"
            f":fontcolor=white@0.92:line_spacing={lay.bullet_size // 4}:x={lay.margin}:y={y}"
            f":alpha='min(1,max(0,(t-{appear:.2f})/0.4))'"
        )
        y += lines * round(lay.bullet_size * 1.35) + lay.bullet_size // 2
    return ",".join(parts)


@dataclass
class Renderer:
    ffmpeg: str
    font: str
    scale: float = 1.0  # доля от 1920×1080 — тестам хватает четверти

    def _run(self, argv: list[str], cancel: threading.Event, what: str) -> None:
        lines: list[str] = []
        try:
            runner.run(argv, lines.append, cancel, missing_hint=FFMPEG_HINT)
        except runner.RunFailed as exc:
            tail = " ".join(x.strip() for x in lines[-3:] if x.strip())
            raise StudioError(f"ffmpeg ({what}): {exc} {tail}".strip()) from exc

    def _encode(self, seconds: float) -> list[str]:
        return ["-t", f"{seconds:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast"]

    def poster_clip(self, image: Path, out: Path, seconds: float, orientation: str, cancel: threading.Event) -> Path:
        lay = layout(orientation, self.scale)
        frames = max(int(seconds * FPS), 1)
        args, chain = _background(image, lay, frames)
        argv = [self.ffmpeg, "-y", "-loglevel", "error", *args, "-filter_complex", f"{chain},format=yuv420p[v]"]
        argv += ["-map", "[v]", *self._encode(seconds), "-an", str(out)]
        self._run(argv, cancel, "постер")
        return out

    def slide_clip(
        self,
        *,
        title: str,
        bullets: list[str],
        image: Path | None,
        out: Path,
        seconds: float,
        orientation: str,
        cancel: threading.Event,
        clip: Path | None = None,
    ) -> Path:
        lay = layout(orientation, self.scale)
        work = out.with_suffix("")
        work.mkdir(parents=True, exist_ok=True)
        title_file = work / "title.txt"
        title_file.write_text(wrap(title, lay.title_chars, 3), encoding="utf-8")
        bullet_files = []
        for n, bullet in enumerate(bullets):
            path = work / f"bullet{n}.txt"
            path.write_text(wrap("• " + bullet, lay.bullet_chars, 2), encoding="utf-8")
            bullet_files.append(path)
        frames = max(int(seconds * FPS), 1)
        args, chain = _background(image, lay, frames, clip)
        text = slide_filter(lay, self.font, title_file, bullet_files, seconds)
        argv = [self.ffmpeg, "-y", "-loglevel", "error", *args]
        argv += ["-filter_complex", f"{chain},{text},format=yuv420p[v]", "-map", "[v]", *self._encode(seconds)]
        argv += ["-an", str(out)]
        self._run(argv, cancel, "слайд")
        return out

    def concat(self, clips: list[Path], audio: Path, out: Path, seconds: float, cancel: threading.Event) -> Path:
        listing = out.with_suffix(".txt")
        listing.write_text("".join(f"file {_quote(c)}\n" for c in clips), encoding="utf-8")
        tmp = out.with_name(out.stem + ".part.mp4")
        argv = [self.ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
        argv += ["-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
        argv += ["-t", f"{seconds:.3f}", "-movflags", "+faststart", str(tmp)]
        self._run(argv, cancel, "склейка")
        tmp.replace(out)
        listing.unlink(missing_ok=True)
        return out

    def build(
        self,
        *,
        poster: Path | None,
        scenes: list[dict],
        audio: Path,
        audio_s: float,
        out: Path,
        orientation: str,
        cancel: threading.Event,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> Path:
        """Видео целиком: постер INTRO_S секунд (если есть), затем сцены {title, bullets, image, seconds}."""
        work = out.parent / f".{out.stem}-clips"
        work.mkdir(parents=True, exist_ok=True)
        clips = []
        total = len(scenes) + (1 if poster else 0)
        done = 0
        if poster is not None:
            clips.append(self.poster_clip(poster, work / "00-poster.mp4", INTRO_S, orientation, cancel))
            done += 1
            if on_progress:
                on_progress(done, total)
        for n, scene in enumerate(scenes, start=1):
            if scene["seconds"] <= 0:
                continue
            clips.append(
                self.slide_clip(
                    title=scene["title"],
                    bullets=scene.get("bullets") or [],
                    image=scene.get("image"),
                    clip=scene.get("clip"),
                    out=work / f"{n:02d}.mp4",
                    seconds=scene["seconds"],
                    orientation=orientation,
                    cancel=cancel,
                )
            )
            done += 1
            if on_progress:
                on_progress(done, total)
        if not clips:
            raise StudioError("Нечего собирать: ни одного кадра.")
        return self.concat(clips, audio, out, audio_s, cancel)


def ffmpeg_works(ffmpeg: str) -> bool:
    try:
        subprocess.run([ffmpeg, "-version"], capture_output=True, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
