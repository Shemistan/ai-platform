#!/usr/bin/env python3
"""Гейт бэклога: шапки задач, их связи, индекс открытых и журнал сделанного.

Бэклог живёт вне git — в `backlog/` главного checkout (в .gitignore), чтобы
агенты из любой ветки и любого worktree видели и правили одно и то же.
Корень без аргумента — главный checkout, найденный через
`git rev-parse --git-common-dir`.

Задача — файл backlog/tasks/<ID>-<slug>.md, ID = <НАПРАВЛЕНИЕ>-<NNN>
(например SEC-001). Плоская YAML-шапка:

    ---
    тип: task
    id: SEC-001
    направление: SEC
    статус: бэклог           # бэклог | в работе | сделано | отменено
    приоритет: P0            # P0 | P1 | P2
    размер: S                # S | M | L
    обновлено: 2026-10-17
    источник: аудит 2026-10-17 / security
    связано: BE-003, QA-002  # ID через запятую или «нет»
    сделано: нет             # при «сделано» — дата YYYY-MM-DD
    pr: нет                  # при «сделано» — номер PR (20) или sha коммита
    ---

Индексы пересобираются в блоках `<!-- gate:begin … -->`:
backlog/README.md (`tasks-index` — открытые по направлениям) и
backlog/CHANGELOG.md (`tasks-done` — сделанные, новые сверху).

Запуск: `python3 tools/tasks_gate.py [корень]` (зовётся из ./check). Нет
каталога backlog/ (свежий клон) — гейт молча пропускается.
"""
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from docs_gate import parse_frontmatter, replace_block, _title  # noqa: E402

BACKLOG = "backlog"
FOLDER = os.path.join(BACKLOG, "tasks")
DIRECTIONS = {
    "SEC": "Безопасность",
    "REL": "Надёжность и эксплуатация",
    "BE": "Бэкенд и данные",
    "FE": "Фронтенд-архитектура",
    "UX": "UX и интерфейс",
    "DS": "Дизайн-система",
    "A11Y": "Доступность",
    "TXT": "Тексты интерфейса",
    "PRD": "Продукт и функционал",
    "AN": "Аналитика",
    "CMP": "Комплаенс и приватность",
    "QA": "Тестирование",
    "PERF": "Производительность и стоимость",
    "DX": "Разработка и документация",
}
STATUSES = ("бэклог", "в работе", "сделано", "отменено")
PRIORITIES = ("P0", "P1", "P2")
SIZES = ("S", "M", "L")
REQUIRED = ("тип", "id", "направление", "статус", "приоритет", "размер",
            "обновлено", "источник", "связано", "сделано", "pr")
NONE = "нет"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ID = re.compile(r"^([A-Z0-9]+)-(\d{3})$")
_PR = re.compile(r"^(\d{1,6}|[0-9a-f]{7,40})$")


class Task:
    def __init__(self, path, meta, body):
        self.path = path
        self.name = os.path.basename(path)
        self.meta = meta
        self.body = body
        self.id = meta.get("id", "")

    def links(self):
        raw = self.meta.get("связано", NONE)
        if raw in ("", NONE):
            return []
        return [x.strip() for x in raw.split(",") if x.strip()]


def collect(root):
    folder = os.path.join(root, FOLDER)
    result = []
    if not os.path.isdir(folder):
        return result
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(FOLDER, name)
        with open(os.path.join(root, path), encoding="utf-8") as f:
            meta, body = parse_frontmatter(f.read())
        result.append(Task(path, meta, body))
    return result


def check(tasks):
    """Список ошибок (путь, сообщение)."""
    errors = []
    ids = {}
    for t in tasks:
        if not t.meta:
            errors.append((t.path, "нет YAML-шапки задачи"))
            continue
        for field in REQUIRED:
            if field not in t.meta:
                errors.append((t.path, "нет обязательного поля '%s'" % field))
        m = t.meta
        if m.get("тип", "task") != "task":
            errors.append((t.path, "тип должен быть 'task'"))
        mid = _ID.match(t.id)
        if not mid:
            errors.append((t.path, "id '%s' не по формату НАПР-NNN" % t.id))
        else:
            if mid.group(1) not in DIRECTIONS:
                errors.append((t.path, "неизвестное направление '%s'; допустимо: %s"
                               % (mid.group(1), ", ".join(DIRECTIONS))))
            if m.get("направление") != mid.group(1):
                errors.append((t.path, "'направление' не совпадает с префиксом id"))
            if not t.name.startswith(t.id + "-"):
                errors.append((t.path, "имя файла должно начинаться с '%s-'" % t.id))
            ids.setdefault(t.id, []).append(t.path)
        for field, allowed in (("статус", STATUSES), ("приоритет", PRIORITIES),
                               ("размер", SIZES)):
            v = m.get(field)
            if v is not None and v not in allowed:
                errors.append((t.path, "'%s' = '%s'; допустимо: %s"
                               % (field, v, ", ".join(allowed))))
        d = m.get("обновлено")
        if d is not None and not _DATE.match(d):
            errors.append((t.path, "'обновлено' — не дата YYYY-MM-DD: '%s'" % d))
        if m.get("статус") == "сделано":
            if not _DATE.match(m.get("сделано", "")):
                errors.append((t.path, "статус 'сделано' требует 'сделано: YYYY-MM-DD'"))
            if not _PR.match(m.get("pr", "")):
                errors.append((t.path, "статус 'сделано' требует 'pr: <номер PR>' или sha коммита"))
        elif m.get("сделано", NONE) != NONE:
            errors.append((t.path, "'сделано' заполнено, а статус не 'сделано'"))
    for tid, paths in sorted(ids.items()):
        if len(paths) > 1:
            errors.append((paths[0], "id %s не уникален: %s" % (tid, ", ".join(paths))))
    for t in tasks:
        for link in t.links():
            if link not in ids:
                errors.append((t.path, "'связано' ссылается на несуществующую задачу %s"
                               % link))
    return errors


def _row(t):
    return "| [%s](%s) | %s | %s | %s | %s |" % (
        t.id, t.name, _title(t.body), t.meta.get("приоритет", "?"),
        t.meta.get("размер", "?"), t.meta.get("статус", "?"))


def render_index(tasks):
    """Открытые задачи (бэклог и в работе) по направлениям, P0 сверху."""
    lines = []
    open_ = [t for t in tasks if t.meta.get("статус") in ("бэклог", "в работе")]
    total = {s: sum(1 for t in tasks if t.meta.get("статус") == s) for s in STATUSES}
    lines.append("Всего: %d · " % len(tasks) + " · ".join(
        "%s %d" % (s, total[s]) for s in STATUSES))
    lines.append("")
    for code, name in DIRECTIONS.items():
        items = sorted((t for t in open_ if t.meta.get("направление") == code),
                       key=lambda t: (t.meta.get("статус") != "в работе",
                                      t.meta.get("приоритет", "P9"), t.id))
        if not items:
            continue
        lines += ["### %s — %s" % (code, name), "",
                  "| ID | Задача | Приор. | Размер | Статус |",
                  "|---|---|---|---|---|"]
        lines += [_row(t) for t in items]
        lines.append("")
    if not open_:
        lines.append("_открытых задач нет_")
    return "\n".join(lines).rstrip()


def render_done(tasks):
    """Журнал сделанного: новые сверху."""
    done = sorted((t for t in tasks if t.meta.get("статус") == "сделано"),
                  key=lambda t: (t.meta.get("сделано", ""), t.id), reverse=True)
    if not done:
        return "_пока нет_"
    return "\n".join("- %s · [%s](tasks/%s) %s · %s" % (
        t.meta["сделано"], t.id, t.name, _title(t.body), _pr(t.meta.get("pr", "")))
        for t in done)


def _pr(value):
    return "PR #" + value if value.isdigit() and len(value) < 7 else value


def regenerate(root, tasks):
    targets = [
        (os.path.join(root, BACKLOG, "README.md"), "tasks-index", render_index(tasks)),
        (os.path.join(root, BACKLOG, "CHANGELOG.md"), "tasks-done", render_done(tasks)),
    ]
    changed = []
    for path, block_id, content in targets:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            old = f.read()
        new = replace_block(old, block_id, content)
        if new is not None and new != old:
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(new)
            except OSError as e:
                # sandbox worktree-сессии не пишет в главный checkout: проверка
                # прошла, индекс пересоберёт следующий запуск из главного checkout
                print("⚠ tasks-gate: индекс %s не пересобран (%s)" % (path, e.strerror),
                      file=sys.stderr)
                continue
            changed.append(path)
    return changed


def main_checkout():
    """Корень главного checkout: из worktree — тот, чей .git общий."""
    try:
        common = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            capture_output=True, text=True, check=True).stdout.strip()
        return os.path.dirname(common)
    except (OSError, subprocess.CalledProcessError):
        return os.getcwd()


def main(argv):
    root = os.path.abspath(argv[1] if len(argv) > 1 else main_checkout())
    if not os.path.isdir(os.path.join(root, FOLDER)):
        return 0
    tasks = collect(root)
    errors = check(tasks)
    if errors:
        print("✗ tasks-gate: бэклог не консистентен:", file=sys.stderr)
        for path, msg in errors:
            print("  %s: %s" % (path, msg), file=sys.stderr)
        return 1
    changed = regenerate(root, tasks)
    if changed:
        print("✓ tasks-gate: индексы пересобраны: %s"
              % ", ".join(os.path.relpath(p, root) for p in changed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
