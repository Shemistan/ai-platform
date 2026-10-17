#!/usr/bin/env python3
"""README платформы не разрастается обратно (DX-015).

После DX-012 `app/platform/README.md` — только установка, запуск и разработка.
Подробности живут в своих документах; гейт падает, если README длиннее
LIMIT строк, и подсказывает, куда переносить.

    python3 tools/readme_gate.py
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = "app/platform/README.md"
LIMIT = 200

# Куда переносить разделы — та же таблица, что в CLAUDE.md «Документация».
WHERE = (
    "как пользоваться вкладками и сценариями — docs/user-guide.md",
    "переменные окружения и настройки — docs/configuration.md",
    "HTTP API — docs/api.md (справочник маршрутов docs/api-routes.md собирается сам)",
    "что делать, когда сломалось на живом сервисе — docs/runbook.md",
    "решения и их причины — ADR в docs/decisions/",
)


def check(path: str, limit: int = LIMIT) -> str | None:
    """Текст ошибки, если README длиннее limit строк; None — всё в порядке."""
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as f:
        n = sum(1 for _ in f)
    if n <= limit:
        return None
    lines = [f"{README}: {n} строк при лимите {limit}. README — только установка,",
             "запуск и разработка; подробности перенеси и оставь ссылку:"]
    lines += [f"  - {w}" for w in WHERE]
    return "\n".join(lines)


def main() -> int:
    err = check(os.path.join(ROOT, README))
    if err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
