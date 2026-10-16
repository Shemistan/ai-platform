"""Имитация сбора новостей narrator для демо и тестов.

`collect --root R --date D` пишет R/D/digest/narration.md, post.md и
image_prompt.md (промпт обложки), как `make news DAY=D`; FAKE_NEWS_COVER=1 —
ещё и готовую cover.png. `where --root R --date D` печатает строку
«Озвучить: …», как `narrate news where`. FAKE_NEWS_EMPTY=1 — выпуск не собрался.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

NEWS = (
    "Главное за день: вышел новый релиз языка Go. В нём ускорили сборщик мусора и добавили итераторы.",
    "Второе событие: облачный провайдер рассказал о сбое. Причина — ошибка в конфигурации балансировщика.",
    "Третье: в сообществе обсуждают новую модель для генерации видео. Она работает локально на ноутбуке.",
    "На этом всё. Подписывайтесь на канал, чтобы не пропустить завтрашний выпуск.",
)
POST = "**Дайджест дня**\n\n— релиз Go\n— сбой в облаке\n— локальная генерация видео\n"
IMAGE_PROMPT = (
    "Create a square 1:1 poster for the cover of a developer news digest. The Go gopher catches a falling cloud.\n"
    'Title: "Дайджест разработчика"\nHeadlines:\n- "Go: новый релиз"\n- "Облако упало"\n'
)


def folder(root: Path, day: str) -> Path:
    return root / day / "digest"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-news")
    parser.add_argument("cmd", choices=("collect", "where"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--date", required=True)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    d = folder(Path(args.root), args.date)
    if args.cmd == "collect":
        for stage in ("обход источников: 12 из 12", "агент пишет дайджест", "судья: готово"):
            print(stage, flush=True)
            time.sleep(float(os.environ.get("FAKE_NEWS_DELAY", "0.05")))
        if os.environ.get("FAKE_NEWS_EMPTY"):
            return 0
        d.mkdir(parents=True, exist_ok=True)
        (d / "narration.md").write_text("\n\n".join(NEWS) + "\n", encoding="utf-8")
        (d / "post.md").write_text(POST, encoding="utf-8")
        (d / "image_prompt.md").write_text(IMAGE_PROMPT, encoding="utf-8")
        if os.environ.get("FAKE_NEWS_COVER"):
            from fake_gen import write_png  # запускается скриптом из каталога fakes

            write_png(str(d / "cover.png"), 96, 96, 3)
        return 0
    if not (d / "narration.md").exists():
        print(f"Дайджеста за {args.date} нет")
        return 0
    print(f"Озвучить: {d / 'narration.md'} → mp3 в {d}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
