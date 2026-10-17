"""Видео дайджеста: статичная картинка-обложка под аудио — для поста одним сообщением.

Telegram показывает видео крупно, с подписью под ним, и играет звук. Чтобы
было видно, что это видео, а не картинка, у него своя обложка: картинка дня
размыта и притемнена, поверх — кнопка «плей» по центру и плашка «подпишись»
внизу. Она же — первый кадр. Дальше идёт
картинка дня с маленькой плашкой в правом нижнем углу. Кадры собирает
Pillow, видео склеивает ffmpeg.

ИИ-контент (CMP-011): на обоих кадрах в левом верхнем углу мелкая плашка
«ИИ» (выключить — NARRATOR_AI_BADGE=0), в mp4 — метка comment=AI-generated.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

VIDEO_SIDE = 1080  # шире не нужно: картинка статичная, а файл растёт
ASSETS = Path(__file__).resolve().parents[3] / "assets" / "news"
PLAY = ASSETS / "play.png"
SUBSCRIBE = ASSETS / "subscribe.png"
# Кадры видео рядом с картинкой дня; find_cover их за картинку не считает.
POSTER = "video-poster.jpg"  # обложка видео и первый кадр: размытый фон, плей, плашка
POSTER_THUMB = "video-thumb.jpg"  # миниатюра обложки: JPEG до 320×320 и 200 КБ
FRAME = "video-frame.png"  # остальные кадры: картинка дня + плашка в углу
NOTE = "video-note.txt"  # пометки о запрещённых организациях, с которыми собран кадр
FRAMES = (POSTER, POSTER_THUMB, FRAME)
PLAY_SHARE = 0.26  # кнопка — доля короткой стороны
POSTER_BLUR = 0.00  # размытие фона обложки: радиус — доля короткой стороны
POSTER_SHADE = 0.75  # затемнение фона обложки: непрозрачность чёрного слоя
SUBSCRIBE_BOTTOM = (0.92, 0.26)  # плашка на обложке: доля ширины, не выше доли высоты
SUBSCRIBE_CORNER = 0.32  # плашка в углу: доля ширины
MARGIN = 0.03  # отступ от края — доля короткой стороны
THUMB_SIDE = 320
NOTE_SHARE = 0.022  # кегль пометки — доля короткой стороны
BADGE = "ИИ"
BADGE_SHARE = 0.03  # кегль плашки «ИИ» — доля короткой стороны
BADGE_ENV = "NARRATOR_AI_BADGE"
AI_METADATA = ["-metadata", "comment=AI-generated", "-metadata", "AI_GENERATED=1"]
FONT_CANDIDATES = (  # нужен шрифт с кириллицей; свой — $NARRATOR_FONT
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)


class VideoError(RuntimeError):
    """ffmpeg не справился или его нет."""


def _fit(img, width: int, max_height: int | None = None):
    from PIL import Image

    height = round(img.height * width / img.width)
    if max_height and height > max_height:
        width, height = round(img.width * max_height / img.height), max_height
    return img.resize((max(width, 1), max(height, 1)), Image.LANCZOS)


def _base(cover: Path):
    """Картинка дня: не шире VIDEO_SIDE, обе стороны чётные — иначе yuv420p не кодируется."""
    from PIL import Image

    with Image.open(cover) as img:
        base = img.convert("RGBA")
    if base.width > VIDEO_SIDE:
        base = _fit(base, VIDEO_SIDE)
    w, h = base.width // 2 * 2, base.height // 2 * 2
    return base.crop((0, 0, w, h))


def _backdrop(base):
    """Фон обложки: картинка дня размыта и притемнена, чтобы плей не терялся в ней."""
    from PIL import Image, ImageFilter

    blurred = base.filter(ImageFilter.GaussianBlur(min(base.size) * POSTER_BLUR))
    shade = Image.new("RGBA", base.size, (0, 0, 0, round(255 * POSTER_SHADE)))
    return Image.alpha_composite(blurred, shade)


def _font(size: int):
    from PIL import ImageFont

    for path in (os.environ.get("NARRATOR_FONT"), *FONT_CANDIDATES):
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def _wrap(text: str, font, width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        if line and font.getlength(probe) > width:
            lines.append(line)
            line = word
        else:
            line = probe
    return lines + ([line] if line else [])


def draw_notes(img, notes: list[str]):
    """Пометки о запрещённых организациях: мелко, во всю ширину внизу, на тёмной полосе.

    Возвращает (кадр, верх полосы)."""
    from PIL import Image, ImageDraw

    w, h = img.size
    size = max(round(min(w, h) * NOTE_SHARE), 10)
    font = _font(size)
    margin = round(min(w, h) * MARGIN)
    lines = [line for note in notes for line in _wrap(note, font, w - 2 * margin)]
    step = round(size * 1.25)
    band = h - margin - len(lines) * step - size // 2
    shade = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(shade).rectangle((0, band, w, h), fill=(0, 0, 0, 140))
    img = Image.alpha_composite(img, shade)
    draw = ImageDraw.Draw(img)
    for n, line in enumerate(lines):
        draw.text((margin, band + size // 2 + n * step), line, font=font, fill=(255, 255, 255, 220))
    return img, band


def badge_on() -> bool:
    return os.environ.get(BADGE_ENV, "1").strip().lower() not in ("0", "off", "false", "no")


def draw_badge(img):
    """Плашка «ИИ» мелко в левом верхнем углу: картинка и озвучка сделаны нейросетью."""
    from PIL import ImageDraw

    w, h = img.size
    size = max(round(min(w, h) * BADGE_SHARE), 10)
    font = _font(size)
    margin = round(min(w, h) * MARGIN)
    draw = ImageDraw.Draw(img)
    left, top, right, bottom = draw.textbbox((margin, margin), BADGE, font=font)
    pad = max(size // 3, 2)
    draw.rectangle((left - pad, top - pad, right + pad, bottom + pad), fill=(0, 0, 0, 150))
    draw.text((margin, margin), BADGE, font=font, fill=(255, 255, 255, 235))
    return img


def make_frames(cover: Path, notes: list[str] | None = None) -> tuple[Path, Path, Path]:
    """Обложка видео, её миниатюра и обычный кадр — рядом с картинкой дня.

    notes — пометки о запрещённых организациях: полосой внизу обычного кадра,
    то есть всего видео после первой секунды. Пересобирает, только если
    картинка, заготовки или этот модуль новее кадров или пометки другие.
    """
    from PIL import Image

    d = cover.parent
    poster, thumb, frame = (d / name for name in FRAMES)
    note_file = d / NOTE
    wanted = "\n".join(notes or [])
    had = note_file.read_text(encoding="utf-8") if note_file.exists() else ""
    newest = max(p.stat().st_mtime for p in (cover, PLAY, SUBSCRIBE, Path(__file__)))
    fresh = all(p.exists() and p.stat().st_mtime >= newest for p in (poster, thumb, frame))
    if fresh and had == wanted:
        return poster, thumb, frame
    base = _base(cover)
    w, h = base.size
    margin = round(min(w, h) * MARGIN)
    with Image.open(PLAY) as play_img, Image.open(SUBSCRIBE) as sub_img:
        play = _fit(play_img.convert("RGBA"), round(min(w, h) * PLAY_SHARE))
        banner = _fit(
            sub_img.convert("RGBA"),
            round(w * SUBSCRIBE_BOTTOM[0]),
            round(h * SUBSCRIBE_BOTTOM[1]),
        )
        corner = _fit(sub_img.convert("RGBA"), round(w * SUBSCRIBE_CORNER))

    img = _backdrop(base)
    img.alpha_composite(play, ((w - play.width) // 2, (h - play.height) // 2))
    img.alpha_composite(banner, ((w - banner.width) // 2, h - banner.height - margin))
    if badge_on():
        img = draw_badge(img)
    img = img.convert("RGB")
    img.save(poster, "JPEG", quality=90)
    small = img.copy()
    small.thumbnail((THUMB_SIDE, THUMB_SIDE))
    small.save(thumb, "JPEG", quality=85)

    img = base.copy()
    if notes:  # плашка в углу — над полосой пометок
        img, band = draw_notes(img, notes)
        lift = h - band
    else:
        lift = 0
    img.alpha_composite(corner, (w - corner.width - margin, h - corner.height - margin - lift))
    if badge_on():
        img = draw_badge(img)
    img.convert("RGB").save(frame, "PNG")
    if notes:
        note_file.write_text(wanted, encoding="utf-8")
    else:
        note_file.unlink(missing_ok=True)
    return poster, thumb, frame


def make_video(poster: Path, frame: Path, audio: Path, out: Path, seconds: float) -> Path:
    """mp4: первый кадр — обложка, дальше кадр с плашкой; звук AAC.

    Кадр раз в секунду: картинка статичная. Пересобирает, если устарел.
    seconds — длина аудио: по ней видео и обрезается. -shortest при одном
    кадре в секунду опаздывает — на пятиминутном выпуске видео выходило
    на 40 секунд длиннее звука.
    """
    newest_input = max(p.stat().st_mtime for p in (poster, frame, audio))
    if out.exists() and out.stat().st_mtime >= newest_input:
        return out
    if seconds <= 0:
        raise VideoError(f"{audio.name}: длина аудио неизвестна — нужен ffprobe")
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-loop", "1", "-framerate", "1", "-i", str(frame),
        "-loop", "1", "-framerate", "1", "-i", str(poster),
        "-i", str(audio),
        "-filter_complex", "[0:v][1:v]overlay=enable='lt(t,1)'[v]",
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "128k", *AI_METADATA,
        "-t", f"{seconds:.3f}", "-movflags", "+faststart",
        str(out),
    ]  # fmt: skip
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
    except FileNotFoundError as e:
        raise VideoError("нет ffmpeg: brew install ffmpeg") from e
    except subprocess.CalledProcessError as e:
        out.unlink(missing_ok=True)
        raise VideoError(f"ffmpeg: {e.stderr.strip()[-300:]}") from e
    return out


def video_size(path: Path) -> tuple[int, int]:
    """Ширина и высота кадра через ffprobe; (0, 0) — не узнать."""
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0"]
    cmd += ["-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        w, h = out.strip().split(",")[:2]
        return int(w), int(h)
    except (OSError, subprocess.CalledProcessError, ValueError):
        return 0, 0
