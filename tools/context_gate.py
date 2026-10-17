#!/usr/bin/env python3
"""Карта проекта docs/context.md не отстаёт от кода (DX-016).

CLAUDE.md импортирует карту — её видит любая сессия агента без чтения
репозитория. Гейт падает, если в карте нет того, что появилось в коде:
сервиса app/<svc>/, пакета или модуля платформы и narrator, вкладки в static/,
сценария или шага студии, ADR. И если карта разрослась больше LIMIT строк.

    python3 tools/context_gate.py
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONTEXT = "docs/context.md"
LIMIT = 300

PLATFORM = "app/platform/aiplatform"
NARRATOR = "app/narrator/src/narrator"
SKIP = {"__pycache__", "__init__.py"}


def _entries(root: str, path: str) -> list[str]:
    """Пакеты (`имя/`) и модули (`имя.py`) каталога, без служебных."""
    full = os.path.join(root, path)
    if not os.path.isdir(full):
        return []
    out = []
    for name in sorted(os.listdir(full)):
        if name in SKIP or name.startswith("."):
            continue
        if os.path.isdir(os.path.join(full, name)):
            out.append(f"{name}/")
        elif name.endswith(".py"):
            out.append(name)
    return out


def _read(root: str, path: str) -> str:
    try:
        with open(os.path.join(root, path), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def required(root: str) -> list[tuple[str, str]]:
    """Что обязано быть в карте: (что это, токен). Токены в обратных кавычках — `text` и т. п.
    слишком общие слова, чтобы искать их голыми."""
    need: list[tuple[str, str]] = []
    apps = os.path.join(root, "app")
    if os.path.isdir(apps):
        need += [("сервис", f"app/{d}/") for d in sorted(os.listdir(apps)) if os.path.isdir(os.path.join(apps, d))]
    need += [("модуль платформы", e) for e in _entries(root, PLATFORM)]
    need += [("вкладка static/", e) for e in _entries(root, f"{PLATFORM}/static") if e.endswith("/")]
    need += [("модуль narrator", e) for e in _entries(root, NARRATOR)]
    pipeline = _read(root, f"{PLATFORM}/studio/pipeline.py")
    need += [("шаг студии", f"`{s}`") for s in re.findall(r'Step\("(\w+)"', pipeline)]
    service = _read(root, f"{PLATFORM}/studio/service.py")
    if m := re.search(r"^SCENARIOS = \{(.*?)\}", service, re.M):
        need += [("сценарий", f"`{s}`") for s in re.findall(r'"(\w+)":', m.group(1))]
    adr = os.path.join(root, "docs/decisions")
    if os.path.isdir(adr):
        need += [("ADR", n[:4]) for n in sorted(os.listdir(adr)) if re.match(r"\d{4}-", n)]
    return need


def check(root: str = ROOT, limit: int = LIMIT) -> str | None:
    """Текст ошибки или None, если карта полна и в лимите."""
    text = _read(root, CONTEXT)
    if not text:
        return f"{CONTEXT}: нет файла — карта проекта для агента (DX-016)."
    errors = []
    missing = [f"{kind}: {token}" for kind, token in required(root) if token not in text]
    if missing:
        errors.append(f"{CONTEXT}: в карте нет того, что есть в коде — допиши строку про каждое:")
        errors += [f"  - {m}" for m in missing]
    n = text.count("\n")
    if n > limit:
        errors.append(f"{CONTEXT}: {n} строк при лимите {limit} — карта, а не документация: сожми, подробности дай ссылкой.")
    return "\n".join(errors) or None


def main() -> int:
    err = check()
    if err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
