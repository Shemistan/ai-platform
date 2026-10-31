"""Имитация сбора новостей narrator для демо и тестов.

`collect --root R --date D` пишет R/D/digest/narration.md, post.md и
image_prompt.md (промпт обложки) и review.md судьи (вердикт —
FAKE_NEWS_VERDICT, по умолчанию «готово»), как `make news DAY=D`; FAKE_NEWS_COVER=1 —
ещё и готовую cover.png. `where --root R --date D` печатает строку
«Озвучить: …», как `narrate news where`. FAKE_NEWS_EMPTY=1 — выпуск не собрался.
`review --root R --kind weekly|monthly --date P` пишет R/reviews/<kind>-<P>/teleprompter.md и post.md, как
`make news-weekly|news-monthly` (PRD-033); `reviews --root R` печатает JSON обзоров, как `narrate bridge reviews`.
"""

from __future__ import annotations

import argparse
import json
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


REVIEW = (
    "Привет! Это обзор: собрал для вас главное за период.",
    "Первая тема — новый релиз Go. Сборщик мусора стал быстрее, а итераторы наконец в языке.",
    "Вторая — сбой у облачного провайдера: виноват конфиг балансировщика.",
    "И третья — локальные модели для видео: теперь они работают на ноутбуке.",
    "Это всё, увидимся в следующем обзоре.",
)


def review_folders(root: Path) -> list[Path]:
    base = root / "reviews"
    return sorted(base.iterdir()) if base.is_dir() else []


def folder(root: Path, day: str) -> Path:
    return root / day / "digest"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-news")
    parser.add_argument("cmd", choices=("collect", "where", "review", "reviews"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--date", default="")
    parser.add_argument("--kind", default="weekly")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    root = Path(args.root)
    if args.cmd == "review":
        print("агент отбирает события и пишет суфлёр", flush=True)
        if os.environ.get("FAKE_NEWS_EMPTY"):
            return 0
        out = root / "reviews" / f"{args.kind}-{args.date or 'now'}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "teleprompter.md").write_text("\n\n".join(REVIEW) + "\n", encoding="utf-8")
        (out / "post.md").write_text("**Обзор**\n\n— релиз Go\n— сбой в облаке\n", encoding="utf-8")
        return 0
    if args.cmd == "reviews":
        items = [{"kind": p.name.split("-")[0], "title": p.name, "folder": str(p)} for p in review_folders(root)]
        print(json.dumps({"reviews": items}, ensure_ascii=False))
        return 0
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
        verdict = os.environ.get("FAKE_NEWS_VERDICT", "готово")
        review = f"# Судья: дайджест за {args.date}\n\nВердикт: {verdict}\n\n| Тема | Новое? |\n|---|---|\n"
        review += "| релиз Go | да |\n"
        (d / "review.md").write_text(review, encoding="utf-8")
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
