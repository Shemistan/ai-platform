#!/usr/bin/env python3
"""Гейт документации: шапки спек/планов/ADR, связи между ними, автоиндексы.

Артефакты: docs/superpowers/specs/*.md (spec), docs/superpowers/plans/*.md
(plan), docs/decisions/*.md (adr); README.md в этих папках не артефакт.
У каждого — плоская YAML-шапка:

    ---
    тип: spec
    статус: черновик
    обновлено: 2026-09-21
    план: 2026-09-21-foo.md      # spec: имя плана или «нет»
    ---

План обязан указывать `спека:`, и ссылки spec↔plan двусторонни. ADR в
статусе «заменено» указывает `заменён_на:`. Индексы пересобираются в блоках
`<!-- gate:begin <id> -->…<!-- gate:end <id> -->` (docs/superpowers/README.md —
`spec-plan-index`, docs/decisions/README.md — `adr-index`); файлы без маркеров
не трогаются. Изменённые индексы добавляются в индекс git.

Запуск: `python3 tools/docs_gate.py [корень]` (зовётся из ./check).
Вынесено из marketing/tools/gate, без столбов roadmap.
"""
import os
import re
import subprocess
import sys

STATUSES = {
    "spec": {"черновик", "утверждено", "реализовано", "неактуально"},
    "plan": {"запланировано", "в работе", "завершено", "брошено"},
    "adr": {"предложено", "принято", "заменено"},
}
FOLDERS = {
    os.path.join("docs", "superpowers", "specs"): "spec",
    os.path.join("docs", "superpowers", "plans"): "plan",
    os.path.join("docs", "decisions"): "adr",
}
REQUIRED = ("тип", "статус", "обновлено")
NONE = "нет"

_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ADR_NUM = re.compile(r"^(\d{4})-")


class Artifact:
    def __init__(self, path, kind, meta, body):
        self.path = path
        self.name = os.path.basename(path)
        self.kind = kind
        self.meta = meta
        self.body = body


def parse_frontmatter(text):
    """(meta, body). Шапка — плоские `ключ: значение`; `#` — комментарий.
    Нет шапки или она не закрыта → ({}, text)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, text
    meta = {}
    for i in range(1, len(lines)):
        line = lines[i]
        if line.strip() == "---":
            return meta, "\n".join(lines[i + 1:])
        s = line.strip()
        if not s or s.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.split("#", 1)[0].strip()
    return {}, text


def collect(root):
    result = []
    for rel, kind in FOLDERS.items():
        folder = os.path.join(root, rel)
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.endswith(".md") or name == "README.md":
                continue
            path = os.path.join(folder, name)
            with open(path, encoding="utf-8") as f:
                meta, body = parse_frontmatter(f.read())
            result.append(Artifact(path, kind, meta, body))
    return result


def check(artifacts):
    """Список ошибок (путь, сообщение)."""
    errors = []
    by = {k: {a.name: a for a in artifacts if a.kind == k} for k in STATUSES}

    for a in artifacts:
        if not a.meta:
            errors.append((a.path, "нет YAML-шапки (--- тип/статус/обновлено ---)"))
            continue
        for field in REQUIRED:
            if field not in a.meta:
                errors.append((a.path, "нет обязательного поля '%s'" % field))
        t = a.meta.get("тип")
        if t is not None and t != a.kind:
            errors.append((a.path, "тип '%s' не совпадает с папкой (%s)" % (t, a.kind)))
        st = a.meta.get("статус")
        if st is not None and st not in STATUSES[a.kind]:
            errors.append((a.path, "неизвестный статус '%s'; допустимо: %s"
                           % (st, ", ".join(sorted(STATUSES[a.kind])))))
        d = a.meta.get("обновлено")
        if d is not None and not _DATE.match(d):
            errors.append((a.path, "'обновлено' — не дата YYYY-MM-DD: '%s'" % d))

    for spec in by["spec"].values():
        plan = spec.meta.get("план")
        if not plan or plan == NONE:
            continue
        target = by["plan"].get(plan)
        if target is None:
            errors.append((spec.path, "план '%s' не существует" % plan))
        elif target.meta.get("спека") != spec.name:
            errors.append((spec.path, "нет обратной ссылки: план '%s' не указывает "
                           "на '%s'" % (plan, spec.name)))

    for plan in by["plan"].values():
        if not plan.meta:
            continue
        spec = plan.meta.get("спека")
        if not spec:
            errors.append((plan.path, "нет обязательного поля 'спека'"))
            continue
        target = by["spec"].get(spec)
        if target is None:
            errors.append((plan.path, "спека '%s' не существует" % spec))
        elif target.meta.get("план") != plan.name:
            errors.append((plan.path, "нет обратной ссылки: спека '%s' не указывает "
                           "на '%s'" % (spec, plan.name)))

    numbers = {}
    for adr in by["adr"].values():
        m = _ADR_NUM.match(adr.name)
        if m:
            numbers.setdefault(m.group(1), []).append(adr.name)
        else:
            errors.append((adr.path, "имя ADR должно начинаться с номера NNNN-"))
        if adr.meta.get("статус") == "заменено":
            to = adr.meta.get("заменён_на")
            if not to or to not in by["adr"]:
                errors.append((adr.path, "статус 'заменено' требует 'заменён_на' "
                               "на существующий ADR (указано: %r)" % to))
    for num, names in sorted(numbers.items()):
        if len(names) > 1:
            errors.append((os.path.join("docs", "decisions", names[0]),
                           "номер ADR %s не уникален: %s" % (num, ", ".join(names))))
    return errors


def _title(body):
    for line in body.splitlines():
        if line.startswith("# "):
            return line[2:].strip().replace("|", "\\|")
    return "(без заголовка)"


def render_spec_plan_index(artifacts):
    lines = []
    for kind, head, folder in (("spec", "### Спеки", "specs"),
                               ("plan", "### Планы", "plans")):
        lines += [head, ""]
        items = sorted((a for a in artifacts if a.kind == kind), key=lambda a: a.name)
        if not items:
            lines.append("_пока нет_")
        for a in items:
            lines.append("- [%s](%s/%s) — %s" % (_title(a.body), folder, a.name,
                                                 a.meta.get("статус", "?")))
        lines.append("")
    return "\n".join(lines).rstrip()


def render_adr_index(artifacts):
    rows = ["| № | Решение | Статус |", "|---|---------|--------|"]
    for a in sorted((a for a in artifacts if a.kind == "adr"), key=lambda a: a.name):
        m = _ADR_NUM.match(a.name)
        rows.append("| [%s](%s) | %s | %s |" % (m.group(1) if m else a.name, a.name,
                                               _title(a.body), a.meta.get("статус", "?")))
    return "\n".join(rows)


def replace_block(text, block_id, content):
    """Текст с заменённым блоком или None, если маркеров нет."""
    begin = "<!-- gate:begin %s -->" % block_id
    end = "<!-- gate:end %s -->" % block_id
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.DOTALL)
    if not pattern.search(text):
        return None
    return pattern.sub(lambda _m: "%s\n%s\n%s" % (begin, content, end), text)


def regenerate(root, artifacts):
    """Пересобрать индексы; вернуть список изменённых файлов."""
    targets = [
        (os.path.join(root, "docs", "superpowers", "README.md"),
         "spec-plan-index", render_spec_plan_index(artifacts)),
        (os.path.join(root, "docs", "decisions", "README.md"),
         "adr-index", render_adr_index(artifacts)),
    ]
    changed = []
    for path, block_id, content in targets:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            old = f.read()
        new = replace_block(old, block_id, content)
        if new is not None and new != old:
            with open(path, "w", encoding="utf-8") as f:
                f.write(new)
            changed.append(path)
    return changed


def main(argv):
    root = os.path.abspath(argv[1] if len(argv) > 1 else os.getcwd())
    artifacts = collect(root)
    errors = check(artifacts)
    if errors:
        print("✗ docs-gate: документация не консистентна:", file=sys.stderr)
        for path, msg in errors:
            print("  %s: %s" % (os.path.relpath(os.path.join(root, path), root), msg),
                  file=sys.stderr)
        return 1
    changed = regenerate(root, artifacts)
    if changed:
        subprocess.run(["git", "-C", root, "add", "--", *changed], check=False)
        print("✓ docs-gate: индексы пересобраны: %s"
              % ", ".join(os.path.relpath(p, root) for p in changed))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
