"""Обложка дайджеста: постер с гофером Go и заголовками дня.

Код кладёт заготовку `image_prompt.md` с оформлением дня (`designs.py`: сезон
месяца и дизайн дня), агент дописывает сцену с гофером по мотивам новостей и
текст на картинке — название, дата и 3–5 коротких заголовков без ссылок.
Человек генерирует по нему картинку в ChatGPT и кладёт её в каталог
дайджеста — имя любое. Здесь картинку находят, называют
`cover.<расширение>` и делают из неё обложку аудио для Telegram.
"""

from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path

PROMPT = "image_prompt.md"
THUMB = "cover-thumb.jpg"  # обложка аудио в Telegram: JPEG до 320×320 и 200 КБ
THUMB_SIDE = 320
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp")
PREVIEW = "preview"  # превью видео (preview.png и т.п.) — не картинка поста
TEXT_BLOCK = "Text on the image"
HEADLINES = (3, 5)  # столько заголовков на постере: больше — мелко и с ошибками
HEADLINE_MAX = 28  # знаков в заголовке: модель рисует короткий текст без опечаток

_QUOTED = re.compile(r'^\s*(?:(Title|Date):|-)\s*"(.+)"\s*$')
_LINKISH = re.compile(r"https?://|www\.|t\.me/|\]\(|\.(?:com|org|ru|dev|io)\b", re.I)


class CoverError(RuntimeError):
    """Картинки для обложки нет."""


def poster_text(prompt: str) -> tuple[str, str, list[str]]:
    """Текст постера из блока «Text on the image»: название, дата, заголовки."""
    title, day, lines = "", "", []
    _, found, block = prompt.partition(TEXT_BLOCK)
    if not found:
        return title, day, lines
    for line in block.splitlines():
        m = _QUOTED.match(line)
        if not m:
            continue
        if m[1] == "Title":
            title = m[2]
        elif m[1] == "Date":
            day = m[2]
        else:
            lines.append(m[2])
    return title, day, lines


def _words(text: str) -> str:
    return " ".join(re.findall(r"\w+", text.lower()))


def check_image_prompt(prompt: str, post: str = "", day: date | None = None) -> list[str]:
    """Ошибки image_prompt.md: нет гофера или текста, ссылки, длинно, повтор post.md.

    С днём выпуска — ещё и оформление: заготовка заполнена, дизайн дня на месте.
    """
    from narrator.news.designs import check_design
    from narrator.news.digest import headlines

    if "gopher" not in prompt:
        return [f"{PROMPT}: нет гофера — героя обложки"]
    title, when, lines = poster_text(prompt)
    if not (title and lines):
        return [
            f"{PROMPT}: нет текста для картинки — блок «{TEXT_BLOCK}» с Title, Date"
            ' и заголовками строками - "…" (шаблон в CLAUDE.md)'
        ]
    problems = [f"{PROMPT}: {p}" for p in check_design(prompt, day)] if day else []
    if any("не заполнено" in p for p in problems):
        return problems
    low, high = HEADLINES
    if not low <= len(lines) <= high:
        problems.append(f"{PROMPT}: {len(lines)} заголовков, нужно {low}–{high}")
    for line in [title, when, *lines]:
        if _LINKISH.search(line):
            problems.append(f"{PROMPT}: «{line}» — ссылкам на картинке не место, они в post.md")
        elif len(line) > HEADLINE_MAX:
            problems.append(
                f"{PROMPT}: «{line}» — {len(line)} знаков, слишком длинно для картинки"
                f" (до {HEADLINE_MAX}): оставь суть в 2–4 словах"
            )
    posted = [_words(h) for h in headlines(post, limit=20)]
    for line in lines:
        words = _words(line)
        if len(words.split()) >= 3 and any(p.startswith(words) for p in posted):
            problems.append(
                f"{PROMPT}: «{line}» повторяет строку post.md — на картинке нужен"
                " свой короткий заголовок, а не текст поста"
            )
    return problems


def find_cover(d: Path, source: Path | None = None) -> Path:
    """Картинка дайджеста: из source или самая свежая в каталоге → cover.<расширение>."""
    if source is not None:
        if not source.is_file() or source.suffix.lower() not in IMAGE_EXT:
            raise CoverError(f"{source}: нет такой картинки (png, jpg, webp)")
        target = d / f"cover{source.suffix.lower()}"
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return target
    from narrator.news.video import FRAMES

    ours = {THUMB, *FRAMES}  # миниатюра, кадры и превью видео — не картинка дня
    found = [
        p
        for p in d.iterdir()
        if p.suffix.lower() in IMAGE_EXT and p.name not in ours and p.stem.lower() != PREVIEW
    ]
    if not found:
        raise CoverError(
            f"в {d} нет картинки: сгенерируй её в ChatGPT по {PROMPT} и положи в папку"
            " (png, jpg, webp; имя любое) или укажи файл: --image <файл>"
        )
    newest = max(found, key=lambda p: p.stat().st_mtime)
    target = d / f"cover{newest.suffix.lower()}"
    if newest != target:
        newest.replace(target)
        print(f"обложка: {newest.name} → {target.name}")
    return target


def thumbnail(cover: Path) -> Path:
    """Обложка аудио рядом с картинкой: середина квадратом, 320×320 JPEG."""
    from PIL import Image

    out = cover.with_name(THUMB)
    with Image.open(cover) as img:
        w, h = img.size
        side = min(w, h)
        box = ((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2)
        square = img.convert("RGB").crop(box).resize((THUMB_SIDE, THUMB_SIDE))
        square.save(out, "JPEG", quality=85)
    return out
