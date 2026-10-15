#!/usr/bin/env python3
"""Валидация сообщения коммита: Conventional Commits + запрет соавторства."""
import re
import sys

TYPES = {"feat", "fix", "docs", "refactor", "test", "chore", "build", "ci",
         "perf", "style"}
_RE = re.compile(r"^(?P<type>[a-z]+)(\([^)]+\))?!?: .+$")
FORBIDDEN = ("co-authored-by", "generated with", "claude-session")
SERVICE_PREFIXES = ("Merge ", "Revert ", "fixup!", "squash!")


def validate(text):
    """Список ошибок (пустой = ок)."""
    errors = []
    lines = [l for l in text.splitlines() if not l.startswith("#")]
    subject = lines[0] if lines else ""

    for l in lines:
        low = l.lower()
        if any(f in low for f in FORBIDDEN):
            errors.append("строка соавторства запрещена")
            break

    if subject.startswith(SERVICE_PREFIXES):
        return errors  # служебные сообщения git не форматируем

    m = _RE.match(subject)
    if not m:
        errors.append("формат сабжа: 'тип(область): описание'")
    else:
        if m.group("type") not in TYPES:
            errors.append("неизвестный тип '%s'; допустимо: %s"
                          % (m.group("type"), ", ".join(sorted(TYPES))))
        if len(subject) > 72:
            errors.append("сабж длиннее 72 символов (%d)" % len(subject))
    return errors


def main():
    if len(sys.argv) < 2:
        print("usage: commit_msg.py <file>", file=sys.stderr)
        return 2
    with open(sys.argv[1], encoding="utf-8") as f:
        errors = validate(f.read())
    if errors:
        print("✗ сообщение коммита не по регламенту:", file=sys.stderr)
        for e in errors:
            print("  - %s" % e, file=sys.stderr)
        print("\n  формат: тип(область): описание — тип из %s"
              % "|".join(sorted(TYPES)), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
