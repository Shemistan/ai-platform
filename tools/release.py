#!/usr/bin/env python3
"""Версия платформы: подготовка релиза и тег (DX-009, схема — docs/README.md «Версии»).

Версия — дата релиза: `ГГГГ.ММ.ДД`, второй и следующие релизы за день —
`ГГГГ.ММ.ДД.N` (N с 2). Тег — `v<версия>`. Одна версия на весь репозиторий:
оба pyproject.toml, `aiplatform.__version__` (её отдают /api/health и
/api/config) и запись narrator в uv.lock.

    python3 tools/release.py prep [--date ГГГГ-ММ-ДД]   # в ветке задачи, едет в main через PR
    python3 tools/release.py tag                        # человек, в main после слияния PR
    python3 tools/release.py current                    # текущая версия

`prep` поднимает версию во всех файлах и вставляет в docs/CHANGELOG.md
заголовок версии со списком задач бэклога из коммитов с прошлого тега.
`tag` ставит аннотированный тег `v<версия>` на HEAD, если версия во всех
файлах одна, в журнале есть её заголовок и такого тега ещё нет.
Make-цели: `make release-prep` и `make release-tag` в app/platform.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PYPROJECTS = ("app/platform/pyproject.toml", "app/narrator/pyproject.toml")
INIT = "app/platform/aiplatform/__init__.py"
UV_LOCK = "app/narrator/uv.lock"
CHANGELOG = "docs/CHANGELOG.md"
MARKER = "<!-- release:next -->"
INITIAL = "0.1.0"  # версия до схемы с датами: тега у неё нет

TASK_ID = re.compile(r"\b([A-Z][A-Z0-9]{1,4}-\d{3})\b")
PYPROJECT_VERSION = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.M)
INIT_VERSION = re.compile(r'^(__version__\s*=\s*")([^"]+)(")', re.M)
LOCK_VERSION = re.compile(r'(\[\[package\]\]\nname = "narrator"\nversion = ")([^"]+)(")')


class ReleaseError(RuntimeError):
    pass


# --- файлы ---


def _read(root: str, rel: str) -> str:
    with open(os.path.join(root, rel), encoding="utf-8") as f:
        return f.read()


def _write(root: str, rel: str, text: str) -> None:
    with open(os.path.join(root, rel), "w", encoding="utf-8") as f:
        f.write(text)


def _files(root: str) -> list[tuple[str, re.Pattern]]:
    out = [(p, PYPROJECT_VERSION) for p in PYPROJECTS] + [(INIT, INIT_VERSION)]
    if os.path.exists(os.path.join(root, UV_LOCK)):
        out.append((UV_LOCK, LOCK_VERSION))
    return out


def versions(root: str) -> dict[str, str]:
    """Версия в каждом файле; нет строки версии — ошибка, а не тихий пропуск."""
    out = {}
    for rel, pattern in _files(root):
        m = pattern.search(_read(root, rel))
        if not m:
            raise ReleaseError(f"{rel}: не нашёл строку версии")
        out[rel] = m.group(2)
    return out


def current(root: str) -> str:
    found = versions(root)
    if len(set(found.values())) != 1:
        listed = ", ".join(f"{k}={v}" for k, v in found.items())
        raise ReleaseError(f"версии расходятся: {listed}")
    return next(iter(found.values()))


def set_version(root: str, version: str) -> None:
    for rel, pattern in _files(root):
        text = _read(root, rel)
        _write(root, rel, pattern.sub(lambda m: m.group(1) + version + m.group(3), text, count=1))


# --- версия ---


def next_version(day: dt.date, tags: list[str], taken: str = "") -> str:
    """Дата релиза; занята тегом (или подготовлена, но без тега) — .2, .3…"""
    base = day.strftime("%Y.%m.%d")
    used = {t.removeprefix("v") for t in tags} | ({taken} if taken else set())
    if base not in used:
        return base
    n = 2
    while f"{base}.{n}" in used:
        n += 1
    return f"{base}.{n}"


# --- git ---


def git(root: str, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)
    if proc.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout.strip()


def release_tags(root: str) -> list[str]:
    return [t for t in git(root, "tag", "--list", "v*").splitlines() if t]


def last_tag(root: str) -> str | None:
    try:
        return git(root, "describe", "--tags", "--abbrev=0", "--match", "v*")
    except ReleaseError:
        return None


def subjects_since(root: str, tag: str | None) -> list[str]:
    rng = [f"{tag}..HEAD"] if tag else ["HEAD"]
    out = git(root, "log", "--no-merges", "--format=%s", *rng)
    return [s for s in out.splitlines() if s]


# --- журнал ---


def task_titles(backlog: str | None) -> dict[str, str]:
    """ID → заголовок задачи из backlog/tasks (бэклог вне git: может не быть)."""
    out: dict[str, str] = {}
    folder = os.path.join(backlog, "tasks") if backlog else ""
    if not folder or not os.path.isdir(folder):
        return out
    for name in sorted(os.listdir(folder)):
        m = TASK_ID.match(name)
        if not m or not name.endswith(".md"):
            continue
        with open(os.path.join(folder, name), encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    out[m.group(1)] = line[2:].strip()
                    break
    return out


def section(version: str, day: dt.date, subjects: list[str], titles: dict[str, str]) -> str:
    """Заголовок версии и задачи из коммитов (по ID в сабже, без повторов)."""
    lines = [f"## {version} — {day.isoformat()}", ""]
    seen: dict[str, str] = {}
    for subject in reversed(subjects):  # git log — новые сверху; задачи — в порядке работы
        for tid in TASK_ID.findall(subject):
            seen.setdefault(tid, titles.get(tid) or subject)
    if seen:
        lines += [f"- {tid} — {title}" for tid, title in seen.items()]
    else:
        lines.append(f"- без задач бэклога: коммитов {len(subjects)}")
    return "\n".join(lines) + "\n"


def insert_section(changelog: str, block: str) -> str:
    if MARKER not in changelog:
        raise ReleaseError(f"{CHANGELOG}: нет маркера {MARKER}")
    return changelog.replace(MARKER, f"{MARKER}\n\n{block.rstrip()}", 1)


def main_backlog(root: str) -> str | None:
    """backlog/ главного checkout: из worktree — рядом с общим .git."""
    try:
        common = git(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    except ReleaseError:
        return None
    return os.path.join(os.path.dirname(common), "backlog")


# --- команды ---


def prep(root: str, day: dt.date, backlog: str | None) -> str:
    tags = release_tags(root)
    now = current(root)
    if now != INITIAL and f"v{now}" not in tags:
        raise ReleaseError(f"версия {now} подготовлена, но не выпущена тегом — сначала make release-tag в main")
    version = next_version(day, tags)
    block = section(version, day, subjects_since(root, last_tag(root)), task_titles(backlog))
    changelog = insert_section(_read(root, CHANGELOG), block)
    set_version(root, version)
    _write(root, CHANGELOG, changelog)
    return version


def tag(root: str) -> str:
    version = current(root)
    if version == INITIAL:
        raise ReleaseError(f"версия {INITIAL} — до схемы с датами: сначала make release-prep")
    name = f"v{version}"
    if name in release_tags(root):
        raise ReleaseError(f"тег {name} уже есть: подготовь следующую версию (make release-prep)")
    if f"## {version} " not in _read(root, CHANGELOG):
        raise ReleaseError(f"в {CHANGELOG} нет заголовка версии {version}")
    if git(root, "status", "--porcelain", "--untracked-files=no"):
        raise ReleaseError("в рабочей копии незакоммиченные правки — тег ставится на чистый HEAD")
    git(root, "tag", "-a", name, "-m", f"release {name}")
    return name


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Версия платформы: prep, tag, current.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prep", help="поднять версию и вставить заголовок в docs/CHANGELOG.md")
    p.add_argument("--date", help="дата релиза ГГГГ-ММ-ДД (по умолчанию сегодня)")
    sub.add_parser("tag", help="поставить тег v<версия> на HEAD")
    sub.add_parser("current", help="текущая версия")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "current":
            print(current(ROOT))
        elif args.cmd == "prep":
            day = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
            version = prep(ROOT, day, main_backlog(ROOT))
            print(f"✓ версия {version}: pyproject, __version__, uv.lock и {CHANGELOG}.")
            print("  Проверь журнал, закоммить (chore(release): …) и веди через PR; после слияния — make release-tag в main.")
        else:
            name = tag(ROOT)
            print(f"✓ тег {name} на HEAD. Отправить: git push origin {name}")
    except ReleaseError as exc:
        print(f"⛔ {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
