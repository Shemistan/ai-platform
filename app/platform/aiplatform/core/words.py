"""Числа в текстах по-русски: согласованная форма слова и разряды (TXT-002).

Близнец `plural` / `fmtNum` / `countOf` из static/ui.js — правила одни.
"""

from __future__ import annotations

NBSP = " "


def plural(n: int, one: str, few: str, many: str) -> str:
    """Форма слова после числа: 1, 21 → one; 2–4, 22 → few; 0, 5–20, 25 → many."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_num(n: int) -> str:
    """1200 → «1 200»: разряды через неразрывный пробел, как Intl.NumberFormat("ru-RU")."""
    return f"{int(n):,}".replace(",", NBSP)


def count_of(n: int, one: str, few: str, many: str) -> str:
    """«5 каналов», «1 200 просмотров»."""
    return f"{fmt_num(n)} {plural(n, one, few, many)}"
