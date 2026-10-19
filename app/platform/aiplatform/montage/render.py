"""Команды ffmpeg монтажа: сегмент с раскладкой планов, склейка, финальный проход, субтитры.

Каждый сегмент — отдельный ffmpeg с точной перемоткой (-ss/-to до -i, с перекодированием):
одинаковые параметры кодирования у всех, поэтому склейка — concat без перекодирования.
Имя файла сегмента — хэш его параметров: после правки плана неизменённые берутся из кэша.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

W, H = 1920, 1080
FPS = 30
FADE_S = 0.03
PIP = 360  # диаметр головы кружком в плане split
PIP_MARGIN = 48
ENCODE = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
          "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]  # fmt: skip
# Цветокоррекция головы. Экран не трогаем: интерфейсы должны остаться своих цветов.
GRADES = {
    "none": ("Без коррекции", ""),
    "punch": ("Контраст и сочность", "eq=contrast=1.06:saturation=1.1:gamma=0.98,unsharp=5:5:0.35"),
    "warm": ("Тёплая", "colorbalance=rs=0.04:gs=0.01:bs=-0.04,eq=contrast=1.04:saturation=1.06"),
}
LOUDNORM = "loudnorm=I=-14:TP=-1.5:LRA=11"  # громкость YouTube
CACHE_VERSION = 1  # поднять, если меняется то, как рисуется сегмент


def frame(t: float) -> float:
    """Время на сетке кадров: сегмент из целых кадров не удлиняется при кодировании."""
    return round(round(t * FPS) / FPS, 4)


@dataclass(frozen=True)
class SegmentSpec:
    start: float
    end: float
    layout: str
    grade: str
    head: Path
    screen: Path | None
    offset: float  # экран: screen(t + offset)

    @property
    def duration(self) -> float:
        return round(self.end - self.start, 3)

    def key(self) -> str:
        parts = [CACHE_VERSION, round(self.start, 3), round(self.end, 3), self.layout, self.grade, self.head.name,
                 self.screen.name if self.screen else None, round(self.offset, 3)]  # fmt: skip
        raw = json.dumps(parts)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _head_chain(grade: str) -> str:
    extra = GRADES[grade][1]
    return f",{extra}" if extra else ""


def mask_argv(ffmpeg: str, out: Path, size: int = PIP) -> list[str]:
    """Круглая маска для головы в углу — один раз на проект."""
    r = size / 2
    lum = f"if(lte((X-{r})*(X-{r})+(Y-{r})*(Y-{r}),{r}*{r}),255,0)"
    return [ffmpeg, "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=black:s={size}x{size}",
            "-vf", f"format=gray,geq=lum='{lum}'", "-frames:v", "1", str(out)]  # fmt: skip


def segment_argv(ffmpeg: str, spec: SegmentSpec, out: Path, mask: Path | None = None) -> list[str]:
    d = spec.duration
    argv = [ffmpeg, "-y", "-loglevel", "error", "-ss", f"{spec.start:.3f}", "-t", f"{d:.3f}", "-i", str(spec.head)]
    uses_screen = spec.layout in ("screen", "split") and spec.screen is not None
    layout = spec.layout if uses_screen else "head"
    if uses_screen:
        s = max(0.0, spec.start + spec.offset)
        argv += ["-ss", f"{s:.3f}", "-t", f"{d:.3f}", "-i", str(spec.screen)]
    if layout == "split":
        argv += ["-loop", "1", "-i", str(mask)]
    cover = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},setsar=1"
    fit = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1"
    # Экран короче нужного (кончилась запись) — держим последний кадр.
    screen_in = f"[1:v]{fit},fps={FPS},tpad=stop_mode=clone:stop_duration={d:.3f}"
    if layout == "head":
        video = f"[0:v]{cover},fps={FPS}{_head_chain(spec.grade)}[v]"
    elif layout == "screen":
        video = f"{screen_in}[v]"
    else:
        head = (
            f"[0:v]crop='min(iw,ih)':'min(iw,ih)',scale={PIP}:{PIP},setsar=1,fps={FPS}{_head_chain(spec.grade)},"
            "format=yuva420p[h]"
        )
        video = (
            f"{screen_in}[bg];{head};[2:v]format=gray,scale={PIP}:{PIP}[m];[h][m]alphamerge[hc];"
            f"[bg][hc]overlay=W-w-{PIP_MARGIN}:H-h-{PIP_MARGIN}:shortest=1[v]"
        )
    fade_out = max(0.0, d - FADE_S)
    audio = f"[0:a]aresample=48000,afade=t=in:d={FADE_S},afade=t=out:st={fade_out:.3f}:d={FADE_S}[a]"
    return [*argv, "-filter_complex", f"{video};{audio}", "-map", "[v]", "-map", "[a]", "-t", f"{d:.3f}",
            *ENCODE, str(out)]  # fmt: skip


def concat_list(paths: list[Path]) -> str:
    lines = []
    for path in paths:
        escaped = str(path).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    return "\n".join(lines) + "\n"


def concat_argv(ffmpeg: str, listing: Path, out: Path) -> list[str]:
    return [
        ffmpeg,
        "-y",
        "-loglevel",
        "error",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(listing),
        "-c",
        "copy",
        str(out),
    ]


def _filter_path(path: Path) -> str:
    """Путь в аргументе фильтра (subtitles=…): экранируем \\ : ' по правилам ffmpeg."""
    return str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


@dataclass(frozen=True)
class Overlay:
    path: Path  # прозрачный MOV (ProRes 4444) от HyperFrames
    start: float
    duration: float


def final_argv(
    ffmpeg: str, joined: Path, out: Path, *, subtitles: Path | None = None, overlays: list[Overlay] | None = None
) -> list[str]:
    """Громкость под YouTube, субтитры в кадре и вставки поверх; без них видео копируется как есть."""
    overlays = overlays or []
    argv = [ffmpeg, "-y", "-loglevel", "error", "-i", str(joined)]
    for item in overlays:
        argv += ["-i", str(item.path)]
    chain: list[str] = []
    label = "0:v"
    for n, item in enumerate(overlays, 1):
        end = item.start + item.duration
        chain.append(f"[{n}:v]setpts=PTS-STARTPTS+{item.start:.3f}/TB[o{n}]")
        chain.append(f"[{label}][o{n}]overlay=enable='between(t,{item.start:.3f},{end:.3f})':eof_action=pass[b{n}]")
        label = f"b{n}"
    if subtitles is not None:
        chain.append(f"[{label}]subtitles=filename='{_filter_path(subtitles)}'[vs]")
        label = "vs"
    if chain:
        argv += ["-filter_complex", ";".join(chain), "-map", f"[{label}]"]
        argv += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"]
    else:
        argv += ["-map", "0:v", "-c:v", "copy"]
    # loudnorm дописывает в хвост доли секунды тишины — -shortest режет звук по видео.
    argv += ["-map", "0:a", "-af", LOUDNORM, "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-shortest",
             "-movflags", "+faststart", str(out)]  # fmt: skip
    return argv


# ---------- субтитры ----------


def chunks(words: list[dict], max_words: int = 3, max_s: float = 1.8) -> list[dict]:
    """Слова результата → короткие куски субтитров: до max_words слов, рвём на паузах и концах фраз."""
    out: list[dict] = []
    current: list[dict] = []
    for word in words:
        if current and (
            len(current) >= max_words
            or word["s"] - current[-1]["e"] > 0.35
            or word["e"] - current[0]["s"] > max_s
            or current[-1]["w"][-1:] in ".!?…"
        ):
            out.append({"text": " ".join(w["w"] for w in current), "s": current[0]["s"], "e": current[-1]["e"]})
            current = []
        current.append(word)
    if current:
        out.append({"text": " ".join(w["w"] for w in current), "s": current[0]["s"], "e": current[-1]["e"]})
    return out


def _srt_time(t: float) -> str:
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def srt(items: list[dict]) -> str:
    return "\n".join(f"{n}\n{_srt_time(c['s'])} --> {_srt_time(c['e'])}\n{c['text']}\n" for n, c in enumerate(items, 1))


def _vtt_time(t: float) -> str:
    return _srt_time(t).replace(",", ".")


def vtt(items: list[dict]) -> str:
    """WebVTT для плееров и YouTube: те же куски, что в SRT."""
    body = "\n".join(f"{_vtt_time(c['s'])} --> {_vtt_time(c['e'])}\n{c['text']}\n" for c in items)
    return "WEBVTT\n\n" + body


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def ass(items: list[dict], font: str) -> str:
    """Субтитры в кадр: крупно, заглавными, белые с чёрной обводкой, внизу по центру."""
    name = Path(font).stem if font else "Arial"
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\nPlayResY: 1080\nWrapStyle: 2\n\n"
        "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV\n"
        f"Style: Main,{name},64,&H00FFFFFF,&H00000000,&H64000000,1,1,4,1,2,120,120,90\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    lines = [f"Dialogue: 0,{_ass_time(c['s'])},{_ass_time(c['e'])},Main,,0,0,0,,{c['text'].upper()}" for c in items]
    return head + "\n".join(lines) + "\n"
