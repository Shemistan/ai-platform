"""Сборка видео ffmpeg'ом: постер, «живые» слайды под озвучку, склейка со звуком.

Слайд-картинка — видео-фон (ролик из внешней модели: по кругу, без своего
звука) или картинка-фон: плавный наезд первые ENTER_S секунд, дальше кадр
стоит — движение только при появлении (PRD-031), затемнение снизу,
заголовок и тезисы, которые появляются по очереди, растянуто по слайду
(drawtext с alpha по времени). Динамические слайды (и картинка без картинки)
— тёмный фон и сцена из motion.py. Внизу кадра — мелкая пометка о
запрещённой в РФ организации, если она упоминается на слайде.
Renderer.animate=False (настройка slide_motion) — без наезда, выезда и роста.

Постер — первый кадр: картинка целиком (не влезла по пропорциям — по бокам
её же размытая копия). С оформлением (Branding), как у видео narrator:
первые COVER_HOLD_S секунд — обложка (постер притемнён, по центру кнопка
«плей», внизу крупная плашка «подпишись»; этот кадр Telegram показывает
превью), затем притемнение и плей гаснут, а плашка плавно уезжает в правый
верхний угол и остаётся там на всех слайдах. За OUTRO_S секунд до конца
ролика кадр снова притемняется и плашка выезжает обратно — крупно вниз по
центру, как на обложке.

Тексты идут через textfile с expansion=none: ни кавычки, ни двоеточия, ни % в тексте
не ломают фильтр. Горизонталь 1920×1080 и вертикаль 1080×1920 — одна сцена,
разная раскладка.
"""

from __future__ import annotations

import struct
import subprocess
import textwrap
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from aiplatform.core import runner
from aiplatform.montage import transcript
from aiplatform.studio import motion
from aiplatform.studio.llm import StudioError
from aiplatform.studio.motion import quote as _quote
from aiplatform.studio.motion import wrap
from aiplatform.studio.slides import DYNAMIC, IMAGE

LANDSCAPE, VERTICAL = "landscape", "vertical"
SIZES = {LANDSCAPE: (1920, 1080), VERTICAL: (1080, 1920)}
# Кроме двух раскладок — любой кадр строкой «ШxВ»: видео дайджеста в пропорциях постера, как у narrator.
POSTER_SIDE = 1080  # ширина кадра «как постер» — как VIDEO_SIDE у narrator
FPS = 25
ENTER_S = 1.5  # наезд на картинку при появлении слайда, дальше кадр стоит
ZOOM_IN = 1.08  # насколько картинка увеличивается за наезд
BACKGROUND = "0x141a26"
ACCENT = "0x7ea2ff"
INTRO_S = 2.0
INTRO_BRANDED_S = 3.0  # обложка, переезд плашки в угол и ещё немного чистого постера
COVER_HOLD_S = 0.8  # обложка с плеем и крупной плашкой — первый кадр и превью в Telegram
MOVE_S = 1.0  # плашка уезжает в угол
OUTRO_S = 2.5  # концовка: плашка возвращается на место обложки и держится до конца
FADE_S = 0.5  # притемнение и плей гаснут
COVER_SHADE = 0.6  # притемнение обложки, чтобы плей не терялся в картинке
PLAY_SHARE = 0.26  # кнопка — доля короткой стороны кадра, как у narrator
STICKER_BOTTOM = (0.92, 0.28)  # плашка на обложке: доля ширины, не выше доли высоты
STICKER_CORNER = {"landscape": 0.24, "square": 0.32, "vertical": 0.5}  # плашка в углу: доля ширины
MARGIN = 0.03  # отступ от края — доля короткой стороны
SILENCE_MIN_S = 0.15  # короче — не пауза; что из них граница фразы, решает studio/timing.py
FFMPEG_HINT = "Нужен ffmpeg с drawtext: brew install ffmpeg-full (путь — в PATH или AIP_FFMPEG)."


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
    note_size: int  # пометка о запрещённой организации: мелко, во всю ширину внизу
    note_chars: int


@dataclass(frozen=True)
class Branding:
    """Оформление канала: плашка «подпишись» и кнопка «плей» (PNG с прозрачностью)."""

    sticker: Path
    play: Path

    @property
    def ratio(self) -> float:
        width, height = png_size(self.sticker)
        return width / height


def png_size(path: Path) -> tuple[int, int]:
    """Ширина и высота PNG из заголовка IHDR."""
    head = path.read_bytes()[:24]
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        raise StudioError(f"{path.name}: не PNG.")
    return struct.unpack(">II", head[16:24])


@dataclass(frozen=True)
class Box:
    width: int
    x: int
    y: int


def sticker_boxes(lay: Layout, ratio: float) -> tuple[Box, Box]:
    """Где плашка на обложке (крупно внизу по центру) и где весь остальной ролик (в правом верхнем углу)."""
    w, h = lay.width, lay.height
    margin = round(min(w, h) * MARGIN)
    big = _even(min(w * STICKER_BOTTOM[0], h * STICKER_BOTTOM[1] * ratio))
    corner = _even(w * STICKER_CORNER[shape(w, h)])
    bottom = Box(big, (w - big) // 2, h - round(big / ratio) - margin)
    return bottom, Box(corner, w - corner - margin, margin)


def _lerp(a: int, b: int, e: str) -> str:
    """От a к b по выражению e (0 → 1) — для координат и ширины плашки, в кавычках для фильтра."""
    return f"'{a}+({b - a})*{e}'"


def outro_start(seconds: float) -> float:
    """С какой секунды последнего слайда плашка едет обратно: за OUTRO_S до конца, не раньше начала."""
    return round(max(seconds - OUTRO_S, 0.0), 3)


def _ease(start: float, seconds: float) -> str:
    """0 → 1 за seconds начиная со start, плавно на концах (smoothstep), — выражение ffmpeg от t."""
    p = f"min(1,max(0,(t-{start:.3f})/{seconds:.3f}))"
    return f"({p})*({p})*(3-2*({p}))"


def shape(w: int, h: int) -> str:
    """Вид кадра для долей плашки: вытянутый вверх, квадратный (±20 %) или широкий."""
    if h > w * 1.2:
        return VERTICAL
    return LANDSCAPE if w > h * 1.2 else "square"


def frame(width: int, height: int) -> str:
    """Кадр «как постер»: ширина POSTER_SIDE, пропорции картинки (не уже 9:16 и не шире 16:9)."""
    if width <= 0 or height <= 0:
        return f"{POSTER_SIDE}x{POSTER_SIDE}"
    ratio = min(max(height / width, 9 / 16), 16 / 9)
    return f"{POSTER_SIDE}x{_even(POSTER_SIDE * ratio)}"


def size_of(orientation: str) -> tuple[int, int]:
    if orientation in SIZES:
        return SIZES[orientation]
    w, _, h = orientation.partition("x")
    try:
        return int(w), int(h)
    except ValueError as exc:
        raise StudioError(f"Кадр «{orientation}»: ждём landscape, vertical или ШxВ.") from exc


def layout(orientation: str, scale: float = 1.0) -> Layout:
    w, h = size_of(orientation)
    w, h = _even(w * scale), _even(h * scale)
    if orientation not in SIZES:  # свой кадр: размеры от короткой стороны, знаков — сколько влезет
        side = min(w, h)
        title, bullet, margin = round(side * 0.055), round(side * 0.036), round(w * 0.06)
        note = max(round(side * 0.017), 8)
        room = w - 2 * margin
        return Layout(
            w, h, title, bullet, margin, round(h * 0.58), round(h * 0.36),
            max(int(room / (title * 0.55)), 10), max(int(room / (bullet * 0.55)), 14),
            note, max(int(room / (note * 0.52)), 30),
        )  # fmt: skip
    if orientation == VERTICAL:
        title, bullet, margin = round(h * 0.036), round(h * 0.024), round(w * 0.07)
        top = round(h * 0.58)
        return Layout(w, h, title, bullet, margin, top, round(h * 0.38), 24, 34, max(round(h * 0.012), 8), 66)
    title, bullet, margin = round(h * 0.062), round(h * 0.04), round(w * 0.05)
    top = round(h * 0.6)
    return Layout(w, h, title, bullet, margin, top, round(h * 0.36), 44, 70, max(round(h * 0.02), 8), 130)


def _even(value: float) -> int:
    return max(2, int(round(value / 2)) * 2)


def _zoompan(frames: int, w: int, h: int, animate: bool) -> str:
    """Картинка на весь слайд: плавный наезд (smoothstep) за ENTER_S секунд и неподвижный кадр до конца."""
    zoom = "1"
    if animate:
        p = f"min(1,on/{max(round(ENTER_S * FPS), 1)})"
        zoom = f"1+{ZOOM_IN - 1:g}*{p}*{p}*(3-2*{p})"
    x, y = "iw/2-(iw/zoom/2)", "ih/2-(ih/zoom/2)"
    return f"zoompan=z='{zoom}':x='{x}':y='{y}':d={frames}:s={w}x{h}:fps={FPS}"


def _background(
    image: Path | None, lay: Layout, frames: int, clip: Path | None = None, animate: bool = True
) -> tuple[list[str], str]:
    """Вход и цепочка фильтров фона: видео (по кругу, без звука), картинка или градиент.

    Картинка — без -loop: один кадр, zoompan растягивает его на длину слайда."""
    w, h = lay.width, lay.height
    if clip is not None:
        args = ["-stream_loop", "-1", "-i", str(clip)]
        chain = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps={FPS},setsar=1"
        return args, chain
    if image is not None:
        args = ["-i", str(image)]
        head = f"[0:v]scale={w * 2}:{h * 2}:force_original_aspect_ratio=increase,crop={w * 2}:{h * 2},setsar=1"
        return args, f"{head},{_zoompan(frames, w, h, animate)},setsar=1"
    args = ["-f", "lavfi", "-i", f"color=c={BACKGROUND}:s={w}x{h}:r={FPS}"]
    chain = f"[0:v]drawbox=x=0:y=0:w=iw*0.012:h=ih:color={ACCENT}@0.9:t=fill,setsar=1"
    return args, chain


def slide_filter(
    lay: Layout, font: str, title_file: Path, bullet_files: list[Path], seconds: float, note_file: Path | None = None
) -> str:
    """Тексты поверх фона: заголовок проявляется первым, тезисы — по очереди за ним.
    Пометка (note_file) видна весь слайд — внизу, на своей тёмной полосе."""
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
    # Тезисы — по одному, растянуто на весь слайд: каждые несколько секунд в кадре новое.
    for path, appear in zip(bullet_files, motion.reveal_times(len(bullet_files), seconds, start=0.8), strict=True):
        lines = path.read_text(encoding="utf-8").count("\n") + 1
        parts.append(
            f"drawtext={font_arg}textfile={_quote(path)}:expansion=none:fontsize={lay.bullet_size}"
            f":fontcolor=white@0.92:line_spacing={lay.bullet_size // 4}:x={lay.margin}:y={y}"
            f":alpha='min(1,max(0,(t-{appear:.2f})/0.4))'"
        )
        y += lines * round(lay.bullet_size * 1.35) + lay.bullet_size // 2
    if note_file is not None:
        parts += note_parts(lay, font, note_file)
    return ",".join(parts)


def note_text(notes: list[str], chars: int) -> str:
    """Пометки для кадра целиком, без «…»: длинная переносится на сколько угодно строк — полоса растёт."""
    return "\n".join("\n".join(textwrap.wrap(" ".join(n.split()), width=chars)) for n in notes if n.strip())


def note_top(lay: Layout, note_file: Path) -> int:
    """Верх тёмной полосы пометки внизу кадра."""
    lines = note_file.read_text(encoding="utf-8").count("\n") + 1
    size = lay.note_size
    return lay.height - round(size * 1.2) - lines * round(size * 1.25) - size // 2


def note_parts(lay: Layout, font: str, note_file: Path) -> list[str]:
    """Пометка о запрещённой организации: мелко, во всю ширину внизу, на своей тёмной полосе."""
    size = lay.note_size
    band = note_top(lay, note_file)
    font_arg = f"fontfile={_quote(font)}:" if font else ""
    return [
        f"drawbox=x=0:y={band}:w=iw:h=ih-{band}:color=black@0.55:t=fill",
        f"drawtext={font_arg}textfile={_quote(note_file)}:expansion=none:fontsize={size}"
        f":fontcolor=white@0.85:line_spacing={size // 4}:x={lay.margin}:y={band + size // 2}",
    ]


AI_BADGE = "ИИ"
AI_METADATA = ["-metadata", "comment=AI-generated", "-metadata", "AI_GENERATED=1"]  # EU AI Act ст. 50


def badge_parts(lay: Layout, font: str, bottom: int | None = None) -> list[str]:
    """Плашка «ИИ» мелко в правом нижнем углу (над полосой пометки, если она есть) — CMP-011."""
    size = max(round(min(lay.width, lay.height) * 0.028), 8)
    margin = round(min(lay.width, lay.height) * MARGIN)
    y = (bottom if bottom is not None else lay.height) - margin - size
    font_arg = f"fontfile={_quote(font)}:" if font else ""
    return [
        f"drawtext={font_arg}text='{AI_BADGE}':expansion=none:fontsize={size}:fontcolor=white@0.9"
        f":box=1:boxcolor=black@0.5:boxborderw={max(size // 3, 2)}:x=w-tw-{margin + size // 3}:y={y}"
    ]


@dataclass(frozen=True)
class Progress:
    """Где слайд в ролике: начало и конец, секунды от начала слайдов, и вся их длина; глава — над полосой."""

    begin: float
    end: float
    total: float
    chapter: str = ""


PROGRESS_STEPS = 16  # ступеней роста полосы за слайд


def progress_parts(lay: Layout, font: str, work: Path, progress: Progress, seconds: float) -> list[str]:
    """Тонкая полоса прогресса по верху кадра и название главы под ней."""
    unit = min(lay.width, lay.height) / 1080
    bar = max(round(6 * unit), 2)
    parts = [f"drawbox=x=0:y=0:w={lay.width}:h={bar}:color=white@0.16:t=fill"]
    total = max(progress.total, 0.001)
    a, b = progress.begin / total, min(progress.end / total, 1.0)
    for k, (t0, t1) in enumerate(motion.windows(0.0, max(seconds - 0.05, 0.05), PROGRESS_STEPS), start=1):
        width = max(round(lay.width * (a + (b - a) * k / PROGRESS_STEPS)), 1)
        parts.append(f"drawbox=x=0:y=0:w={width}:h={bar}:color={motion.ACCENT}:t=fill:enable='{motion.window(t0, t1)}'")
    if progress.chapter:
        size = max(round(28 * unit), 10)
        label = work / "chapter.txt"
        label.write_text(wrap(progress.chapter.upper(), lay.title_chars * 2, 1), encoding="utf-8")
        font_opt = f"fontfile={_quote(font)}:" if font else ""
        parts.append(
            f"drawtext={font_opt}textfile={_quote(label)}:expansion=none:fontsize={size}:fontcolor=white@0.75"
            f":x={lay.margin}:y={bar + round(14 * unit)}:box=1:boxcolor=black@0.35:boxborderw={round(8 * unit)}"
        )
    return parts


@dataclass
class Renderer:
    ffmpeg: str
    font: str
    scale: float = 1.0  # доля от 1920×1080 — тестам хватает четверти
    ai_badge: bool = False  # плашка «ИИ» на кадрах (настройка выпуска ai_badge, CMP-011)
    animate: bool = True  # анимация появления слайдов (настройка slide_motion, PRD-031); False — без движения

    def _badge(self, lay: Layout, note_file: Path | None) -> list[str]:
        if not self.ai_badge:
            return []
        return badge_parts(lay, self.font, note_top(lay, note_file) if note_file is not None else None)

    def _run(
        self, argv: list[str], cancel: threading.Event, what: str, on_line: Callable[[str], None] | None = None
    ) -> None:
        lines: list[str] = []

        def line(text: str) -> None:
            lines.append(text)
            if on_line is not None:
                on_line(text)

        try:
            runner.run(argv, line, cancel, missing_hint=FFMPEG_HINT, kind="render")
        except runner.RunFailed as exc:
            tail = " ".join(x.strip() for x in lines[-3:] if x.strip())
            raise StudioError(f"ffmpeg ({what}): {exc} {tail}".strip()) from exc

    def _encode(self, seconds: float) -> list[str]:
        return ["-t", f"{seconds:.3f}", "-r", str(FPS), "-c:v", "libx264", "-preset", "veryfast"]

    def poster_clip(
        self,
        image: Path,
        out: Path,
        seconds: float,
        orientation: str,
        cancel: threading.Event,
        branding: Branding | None = None,
        notes: list[str] | None = None,
    ) -> Path:
        """Постер на seconds секунд. notes — пометки о запрещённых организациях полосой внизу кадра."""
        lay = layout(orientation, self.scale)
        w, h = lay.width, lay.height
        args = ["-loop", "1", "-framerate", str(FPS), "-i", str(image)]
        # Картинка целиком поверх своей размытой копии: квадратная обложка narrator не обрезается.
        graph = [
            "[0:v]split[pa][pb]",
            f"[pa]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},gblur=sigma={max(w // 60, 1)},"
            f"drawbox=x=0:y=0:w=iw:h=ih:color=black@0.35:t=fill[bg]",
            f"[pb]scale={w}:{h}:force_original_aspect_ratio=decrease,setsar=1[fg]",
            "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]",
        ]
        last = "base"
        if branding is not None:
            args += ["-loop", "1", "-framerate", str(FPS), "-i", str(branding.play)]
            args += ["-loop", "1", "-framerate", str(FPS), "-i", str(branding.sticker)]
            play = _even(min(w, h) * PLAY_SHARE)
            bottom, corner = sticker_boxes(lay, branding.ratio)
            e = _ease(COVER_HOLD_S, MOVE_S)
            fade = f"fade=t=out:st={COVER_HOLD_S}:d={FADE_S}:alpha=1"
            graph += [
                f"color=c=black@{COVER_SHADE}:s={w}x{h}:r={FPS},format=rgba,{fade}[shade]",
                "[base][shade]overlay=0:0:shortest=1[shaded]",
                f"[1:v]format=rgba,scale={play}:-1,{fade}[play]",
                "[shaded][play]overlay=(W-w)/2:(H-h)/2:shortest=1[played]",
                f"[2:v]format=rgba,scale=w={_lerp(bottom.width, corner.width, e)}:h=-1:eval=frame[sticker]",
                f"[played][sticker]overlay=x={_lerp(bottom.x, corner.x, e)}:y={_lerp(bottom.y, corner.y, e)}"
                ":shortest=1[branded]",
            ]
            last = "branded"
        note_file = None
        if notes:
            note_file = out.with_name(f"{out.stem}-note.txt")
            note_file.write_text(note_text(notes, lay.note_chars), encoding="utf-8")
            graph.append(f"[{last}]{','.join(note_parts(lay, self.font, note_file))}[noted]")
            last = "noted"
        if self.ai_badge:
            graph.append(f"[{last}]{','.join(self._badge(lay, note_file))}[badged]")
            last = "badged"
        graph.append(f"[{last}]format=yuv420p[v]")
        argv = [self.ffmpeg, "-y", "-loglevel", "error", *args, "-filter_complex", ";".join(graph)]
        argv += ["-map", "[v]", *self._encode(seconds), "-an", str(out)]
        self._run(argv, cancel, "постер")
        return out

    def retime(self, audio: Path, out: Path, tempo: float, cancel: threading.Event) -> Path:
        """Озвучка в другом темпе, как retime_mp3 у narrator: длина делится на tempo, высота голоса та же."""
        tmp = out.with_name(out.stem + ".part.mp3")
        argv = [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(audio), "-filter:a", f"atempo={tempo:g}"]
        argv += ["-codec:a", "libmp3lame", "-b:a", "128k", *AI_METADATA, str(tmp)]
        self._run(argv, cancel, f"скорость ×{tempo:g}")
        tmp.replace(out)
        return out

    def silences(self, audio: Path, cancel: threading.Event) -> list[tuple[float, float]]:
        """Паузы в озвучке (начало, конец) — по ним слайды встают на свои фразы (studio/timing.py)."""
        lines: list[str] = []
        argv = transcript.silence_argv(self.ffmpeg, audio, min_s=SILENCE_MIN_S)
        self._run(argv, cancel, "паузы в озвучке", lines.append)
        return transcript.parse_silences(lines)

    def tag_ai(self, audio: Path, cancel: threading.Event) -> Path:
        """Метка ИИ в mp3 без перекодирования: ID3 TXXX:AI_GENERATED=1 и комментарий (CMP-011)."""
        if audio.suffix.lower() != ".mp3":
            return audio
        tmp = audio.with_name(audio.stem + ".tag.mp3")
        argv = [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(audio), "-map", "0:a", "-c", "copy"]
        argv += [*AI_METADATA, "-id3v2_version", "3", str(tmp)]
        try:
            self._run(argv, cancel, "метка ИИ в mp3")
        except StudioError:
            tmp.unlink(missing_ok=True)
            raise
        tmp.replace(audio)
        return audio

    def cover_frames(self, video: Path, cover: Path, thumb: Path, cancel: threading.Event) -> None:
        """Первый кадр ролика — обложка для Telegram — и её миниатюра (до 320 px, JPEG)."""
        argv = [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(video), "-frames:v", "1", "-q:v", "3", str(cover)]
        self._run(argv, cancel, "обложка")
        small = "scale='if(gt(iw,ih),320,-2)':'if(gt(iw,ih),-2,320)'"
        argv = [self.ffmpeg, "-y", "-loglevel", "error", "-i", str(cover), "-vf", small, "-q:v", "5", str(thumb)]
        self._run(argv, cancel, "миниатюра обложки")

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
        notes: list[str] | None = None,
        branding: Branding | None = None,
        kind: str = IMAGE,
        data: dict | None = None,
        progress: Progress | None = None,
        outro: bool = False,
    ) -> Path:
        """Слайд. kind — вид из плана: картинка (без картинки и ролика — пункты на тёмном фоне)
        или динамическая сцена; data — её данные. progress — полоса ролика с названием главы.
        outro — последний слайд ролика: с оформлением плашка в конце возвращается как на обложке."""
        lay = layout(orientation, self.scale)
        work = out.with_suffix("")
        work.mkdir(parents=True, exist_ok=True)
        for old in work.glob("*.txt"):
            old.unlink()
        note_file = None
        if notes:
            note_file = work / "note.txt"
            note_file.write_text(note_text(notes, lay.note_chars), encoding="utf-8")
        frames = max(int(seconds * FPS), 1)
        # Концовка закрывает слайд плашкой — пункты и элементы сцены успевают показаться до неё.
        ending = branding is not None and outro and outro_start(seconds) >= 1.0
        shown = outro_start(seconds) if ending else seconds
        if kind in DYNAMIC or (image is None and clip is None):
            args, chain = _background(None, lay, frames)
            scene = (data or {}) | {"kind": kind if kind in DYNAMIC else "bullets", "title": title, "bullets": bullets}
            text = self._dynamic(lay, work, scene, shown, note_file, branding)
        else:
            title_file = work / "title.txt"
            title_file.write_text(wrap(title, lay.title_chars, 3), encoding="utf-8")
            bullet_files = []
            for n, bullet in enumerate(bullets):
                path = work / f"bullet{n}.txt"
                path.write_text(wrap("• " + bullet, lay.bullet_chars, 2), encoding="utf-8")
                bullet_files.append(path)
            args, chain = _background(image, lay, frames, clip, self.animate)
            text = slide_filter(lay, self.font, title_file, bullet_files, shown, note_file)
        text = ",".join([text, *self._badge(lay, note_file)])
        if progress is not None:
            text = ",".join([text, *progress_parts(lay, self.font, work, progress, seconds)])
        graph = f"{chain},{text},format=yuv420p[v]"
        if branding is not None and outro:  # концовка: притемнение и плашка из угла крупно вниз
            args += ["-loop", "1", "-framerate", str(FPS), "-i", str(branding.sticker)]
            bottom, corner = sticker_boxes(lay, branding.ratio)
            start = outro_start(seconds)
            e = _ease(start, MOVE_S)
            graph = ";".join(
                [
                    f"{chain},{text}[base]",
                    f"color=c=black@{COVER_SHADE}:s={lay.width}x{lay.height}:r={FPS},format=rgba,"
                    f"fade=t=in:st={start}:d={FADE_S}:alpha=1[shade]",
                    "[base][shade]overlay=0:0:shortest=1[shaded]",
                    f"[1:v]format=rgba,scale=w={_lerp(corner.width, bottom.width, e)}:h=-1:eval=frame[sticker]",
                    f"[shaded][sticker]overlay=x={_lerp(corner.x, bottom.x, e)}:y={_lerp(corner.y, bottom.y, e)}"
                    ":shortest=1,format=yuv420p[v]",
                ]
            )
        elif branding is not None:  # плашка «подпишись» в углу — весь слайд
            args += ["-loop", "1", "-framerate", str(FPS), "-i", str(branding.sticker)]
            _, corner = sticker_boxes(lay, branding.ratio)
            graph = (
                f"{chain},{text}[base];[1:v]format=rgba,scale={corner.width}:-1[sticker];"
                f"[base][sticker]overlay={corner.x}:{corner.y}:shortest=1,format=yuv420p[v]"
            )
        argv = [self.ffmpeg, "-y", "-loglevel", "error", *args]
        argv += ["-filter_complex", graph, "-map", "[v]", *self._encode(seconds)]
        argv += ["-an", str(out)]
        self._run(argv, cancel, "слайд")
        return out

    def _dynamic(
        self, lay: Layout, work: Path, scene: dict, seconds: float, note_file: Path | None, branding: Branding | None
    ) -> str:
        """Фильтры динамической сцены: сверху — место под плашку канала, снизу — под пометку."""
        w, h = lay.width, lay.height
        top = round(h * (0.1 if w > h else 0.12))
        if branding is not None:
            _, corner = sticker_boxes(lay, branding.ratio)
            top = max(top, corner.y + round(corner.width / branding.ratio) + round(min(w, h) * MARGIN))
        bottom = round(h * 0.92)
        if note_file is not None:
            bottom = min(bottom, note_top(lay, note_file) - round(min(w, h) * MARGIN))
        canvas = motion.Canvas(
            w=w, h=h, margin=lay.margin, font=self.font, work=work, top=top, bottom=bottom, animate=self.animate
        )
        parts = [motion.draw(canvas, scene, seconds)]
        if note_file is not None:
            parts += note_parts(lay, self.font, note_file)
        return ",".join(parts)

    def concat(self, clips: list[Path], audio: Path, out: Path, seconds: float, cancel: threading.Event) -> Path:
        listing = out.with_suffix(".txt")
        listing.write_text("".join(f"file {_quote(c)}\n" for c in clips), encoding="utf-8")
        tmp = out.with_name(out.stem + ".part.mp4")
        argv = [self.ffmpeg, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(listing)]
        argv += ["-i", str(audio), "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", "-b:a", "160k"]
        argv += [*AI_METADATA, "-t", f"{seconds:.3f}", "-movflags", "+faststart", str(tmp)]
        self._run(argv, cancel, "склейка")
        tmp.replace(out)
        listing.unlink(missing_ok=True)
        return out

    def still(
        self,
        *,
        poster: Path,
        audio: Path,
        audio_s: float,
        out: Path,
        orientation: str,
        cancel: threading.Event,
        branding: Branding | None = None,
        notes: list[str] | None = None,
    ) -> Path:
        """Пост: постер целиком на всю длину звука; с оформлением — плей и плашка, как у обложки.
        notes — пометки о запрещённых организациях из текста озвучки: полосой внизу всё видео."""
        work = out.parent / f".{out.stem}-clips"
        work.mkdir(parents=True, exist_ok=True)
        clip = self.poster_clip(poster, work / "still.mp4", audio_s, orientation, cancel, branding, notes)
        return self.concat([clip], audio, out, audio_s, cancel)

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
        branding: Branding | None = None,
    ) -> Path:
        """Видео целиком: постер intro_seconds(branding) секунд (если есть), затем сцены.

        Сцена — {title, bullets, image, clip, notes, seconds, kind, data}.
        """
        work = out.parent / f".{out.stem}-clips"
        work.mkdir(parents=True, exist_ok=True)
        clips = []
        total = len(scenes) + (1 if poster else 0)
        done = 0
        if poster is not None:
            intro = intro_seconds(branding)
            clips.append(self.poster_clip(poster, work / "00-poster.mp4", intro, orientation, cancel, branding))
            done += 1
            if on_progress:
                on_progress(done, total)
        last = max((n for n, scene in enumerate(scenes, start=1) if scene["seconds"] > 0), default=0)
        for n, scene in enumerate(scenes, start=1):
            if scene["seconds"] <= 0:
                continue
            clips.append(
                self.slide_clip(
                    title=scene["title"],
                    bullets=scene.get("bullets") or [],
                    image=scene.get("image"),
                    clip=scene.get("clip"),
                    notes=scene.get("notes"),
                    branding=branding,
                    kind=scene.get("kind") or IMAGE,
                    data=scene.get("data"),
                    progress=scene.get("progress"),
                    outro=n == last,
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


def intro_seconds(branding: Branding | None) -> float:
    return INTRO_BRANDED_S if branding is not None else INTRO_S


def ffmpeg_works(ffmpeg: str) -> bool:
    try:
        subprocess.run([ffmpeg, "-version"], capture_output=True, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False
