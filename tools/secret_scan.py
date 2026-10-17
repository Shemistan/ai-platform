#!/usr/bin/env python3
"""Секрет-скан отслеживаемых файлов: токены Telegram-ботов (SEC-001).

Ищет строки вида `<8–10 цифр>:<35 символов [A-Za-z0-9_-]>` в рабочем дереве
отслеживаемых файлов и в индексе (то, что уйдёт в коммит). Синтетика
пропускается: секретная часть из одного повторённого символа
(`12345678:` + 35×`A`) или числовая часть из одной повторённой цифры.

Запуск: `python3 tools/secret_scan.py [корень]` (зовётся из ./check).
Нашёл — печатает `файл:строка` (сам секрет маскирует) и выходит с кодом 1.
"""
import re
import subprocess
import sys

TOKEN_RE = re.compile(r"(?<![0-9])([0-9]{8,10}):([A-Za-z0-9_-]{35})(?![A-Za-z0-9_-])")
GREP_PATTERN = r"[0-9]{8,10}:[A-Za-z0-9_-]{35}"


def is_synthetic(digits, secret):
    """Явно синтетическое значение: один символ, повторённый по всей части."""
    return len(set(secret)) == 1 or len(set(digits)) == 1


def find_tokens(text):
    """Несинтетические токены в тексте: список (номер строки, маска)."""
    found = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in TOKEN_RE.finditer(line):
            digits, secret = m.group(1), m.group(2)
            if is_synthetic(digits, secret):
                continue
            found.append((lineno, f"{digits[:3]}…:{secret[:3]}…"))
    return found


def _grep(root, cached):
    """Кандидаты от `git grep`: {путь: [номера строк]}; бинарники пропускаем."""
    cmd = ["git", "-C", root, "grep", "-I", "-n", "-E", GREP_PATTERN]
    if cached:
        cmd.insert(4, "--cached")
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode not in (0, 1):
        raise SystemExit(f"secret_scan: git grep упал: {res.stderr.strip()}")
    hits = []
    for row in res.stdout.splitlines():
        path, lineno, line = row.split(":", 2)
        for _, mask in find_tokens(line):
            hits.append((path, int(lineno), mask))
    return hits


def scan(root="."):
    """Находки в рабочем дереве и индексе без дублей."""
    hits = set(_grep(root, cached=False)) | set(_grep(root, cached=True))
    return sorted(hits)


def main(argv):
    root = argv[1] if len(argv) > 1 else "."
    hits = scan(root)
    if not hits:
        return 0
    print("⛔ secret_scan: похоже на токен Telegram-бота в отслеживаемых файлах:",
          file=sys.stderr)
    for path, lineno, mask in hits:
        print(f"  {path}:{lineno}  {mask}", file=sys.stderr)
    print("  Настоящий токен — только в bot_token.md/env (вне git). В тестах —\n"
          "  синтетика вида `123456:` + 35×`A`. Утёк в историю — отзывай в"
          " @BotFather.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
