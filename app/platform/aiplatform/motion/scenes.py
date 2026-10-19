"""Время сцен и пакет для монтажки: слова ↔ секунды, таймкоды, csv/txt/json, zip.

Сцена хранит время в секундах исходника (`at`, `end`); таймкод `ЧЧ:ММ:СС:КК` — по частоте кадров
исходника, его автор вводит в своём монтажном приложении.
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "h",
        "ц": "c",
        "ч": "ch",
        "ш": "sh",
        "щ": "sch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }  # fmt: skip
)
LAYOUTS = {
    "full": "Весь кадр",
    "side": "Половина кадра",
    "lower": "Нижняя треть",
}


def slug(text: str, limit: int = 32) -> str:
    """Латинское имя файла из заголовка сцены: «Буфер канала» → bufer-kanala."""
    raw = re.sub(r"[^a-z0-9]+", "-", (text or "").lower().translate(_TRANSLIT)).strip("-")
    return raw[:limit].rstrip("-") or "scene"


def timecode(seconds: float, fps: float) -> str:
    """ЧЧ:ММ:СС:КК без пропуска кадров, как его показывает монтажка: номер кадра — по настоящей частоте
    (29.97 — это 29.97 кадра в секунду), в «секунде» таймкода — округлённое число кадров (30)."""
    base = max(1, round(fps or 30))
    total = round(max(0.0, seconds) * (fps or 30))
    frames = total % base
    s = total // base
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}:{frames:02d}"


def clock(seconds: float) -> str:
    """М:СС или Ч:ММ:СС — для людей и промптов."""
    s = int(max(0.0, seconds))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def window(words: list[dict], first: int, last: int, pad: int = 0) -> list[dict]:
    """Слова [first..last] с запасом pad слов по краям (индексы — в общем списке)."""
    lo, hi = max(0, first - pad), min(len(words) - 1, last + pad)
    return [{**words[i], "i": i} for i in range(lo, hi + 1)]


def word_index_at(words: list[dict], t: float) -> int:
    """Индекс слова, которое звучит в момент t (или ближайшего после)."""
    for i, w in enumerate(words):
        if w["e"] >= t:
            return i
    return max(0, len(words) - 1)


def file_name(scene: dict, fps: float) -> str:
    stamp = timecode(scene["at"], fps).replace(":", "-")
    return f"{scene['n']:02d}_{stamp[:8]}_{slug(scene.get('title', ''))}.mov"


def rows(scenes: list[dict], fps: float) -> list[dict]:
    """Строки таблицы таймкодов: включённые сцены с готовым роликом, по времени."""
    out = []
    for scene in sorted(scenes, key=lambda s: s["at"]):
        if not scene.get("enabled", True) or scene.get("status") != "done":
            continue
        out.append({
            "n": scene["n"],
            "file": file_name(scene, fps),
            "start": timecode(scene["at"], fps),
            "end": timecode(scene["end"], fps),
            "duration": round(scene["end"] - scene["at"], 2),
            "layout": LAYOUTS.get(scene.get("layout", ""), scene.get("layout", "")),
            "title": scene.get("title", ""),
            "about": scene.get("concept", ""),
        })  # fmt: skip
    return out


def csv_text(items: list[dict]) -> str:
    buf = io.StringIO()
    fields = ["n", "file", "start", "end", "duration", "layout", "title", "about"]
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(items)
    return buf.getvalue()


def txt_text(items: list[dict], title: str, fps: float) -> str:
    lines = [f"{title}", f"Таймкоды по частоте исходника: {fps:g} к/с. Ролики — MOV ProRes 4444 с альфой.", ""]
    for row in items:
        lines.append(f"{row['start']} → {row['end']}  {row['file']}")
        lines.append(f"    {row['layout']}. {row['title']}" + (f" — {row['about']}" if row["about"] else ""))
    return "\n".join(lines) + "\n"


def package(target: Path, items: list[dict], movs: dict[int, Path], title: str, fps: float, meta: dict) -> Path:
    """zip: ролики под именами из таблицы, timecodes.csv/txt и scenes.json. MOV не сжимаются (ZIP_STORED)."""
    tmp = target.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w") as zf:
        for row in items:
            mov = movs.get(row["n"])
            if mov and mov.is_file():
                zf.write(mov, row["file"], compress_type=zipfile.ZIP_STORED)
        zf.writestr("timecodes.csv", "﻿" + csv_text(items), compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("timecodes.txt", txt_text(items, title, fps), compress_type=zipfile.ZIP_DEFLATED)
        data = json.dumps({**meta, "fps": fps, "scenes": items}, ensure_ascii=False, indent=2)
        zf.writestr("scenes.json", data, compress_type=zipfile.ZIP_DEFLATED)
    tmp.replace(target)
    return target
