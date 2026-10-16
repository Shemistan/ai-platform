"""Видео дайджеста: статичная картинка-обложка под аудио — для поста одним сообщением.

Telegram показывает видео крупно, с подписью под ним, и играет звук. Чтобы
было видно, что это видео, а не картинка, у него своя обложка: картинка дня
размыта и притемнена, поверх — кнопка «плей» по центру и плашка «подпишись»
внизу. Она же — первый кадр. Дальше идёт
картинка дня с маленькой плашкой в правом нижнем углу. Кадры собирает
Pillow, видео склеивает ffmpeg.
"""

from __future__ import annotations

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
FRAMES = (POSTER, POSTER_THUMB, FRAME)
PLAY_SHARE = 0.26  # кнопка — доля короткой стороны
POSTER_BLUR = 0.00  # размытие фона обложки: радиус — доля короткой стороны
POSTER_SHADE = 0.75  # затемнение фона обложки: непрозрачность чёрного слоя
SUBSCRIBE_BOTTOM = (0.92, 0.26)  # плашка на обложке: доля ширины, не выше доли высоты
SUBSCRIBE_CORNER = 0.32  # плашка в углу: доля ширины
MARGIN = 0.03  # отступ от края — доля короткой стороны
THUMB_SIDE = 320


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


def make_frames(cover: Path) -> tuple[Path, Path, Path]:
    """Обложка видео, её миниатюра и обычный кадр — рядом с картинкой дня.

    Пересобирает, только если картинка, заготовки или этот модуль новее кадров.
    """
    from PIL import Image

    d = cover.parent
    poster, thumb, frame = (d / name for name in FRAMES)
    newest = max(p.stat().st_mtime for p in (cover, PLAY, SUBSCRIBE, Path(__file__)))
    if all(p.exists() and p.stat().st_mtime >= newest for p in (poster, thumb, frame)):
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
    img = img.convert("RGB")
    img.save(poster, "JPEG", quality=90)
    small = img.copy()
    small.thumbnail((THUMB_SIDE, THUMB_SIDE))
    small.save(thumb, "JPEG", quality=85)

    img = base.copy()
    img.alpha_composite(corner, (w - corner.width - margin, h - corner.height - margin))
    img.convert("RGB").save(frame, "PNG")
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
        "-c:a", "aac", "-b:a", "128k",
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
