"""Промпты монтажа для LLM и разбор ответов: план, проверка результата, замечание человека.

Модель видит расшифровку фразами — `[номер первого слова] время текст`, паузы
отдельной строкой — и отвечает индексами слов, а не секундами: время берётся из
расшифровки. Первая строка промпта — `### task: montage_*` (по ней отвечает фейк).
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from aiplatform.montage import edl

ASSETS = Path(__file__).resolve().parent / "assets"
RULES = ASSETS / "rules.md"
PHRASE_GAP_S = 0.45
SHOW_PAUSE_S = 1.0
_SENTENCE_END = re.compile(r"[.!?…]$")
LESSONS_TEMPLATE = """# Выученные правила монтажа

Это дополнение к базовым правилам. Сюда сами попадают выводы из замечаний к роликам;
правь как удобно.

## Термины

Английские слова и имена, которые звучат в роликах: подсказка расшифровке. По одному в строке.

- Claude Code
- ffmpeg
- GitHub

## Правила
"""


def pack(words: list[dict], *, offset: int = 0) -> str:
    """Расшифровка для LLM: фраза в строке, `[номер] время текст`; длинные паузы — отдельной строкой."""
    lines: list[str] = []
    current: list[str] = []
    first = 0
    for i, word in enumerate(words):
        if current:
            gap = word["s"] - words[i - 1]["e"]
            if gap >= PHRASE_GAP_S or _SENTENCE_END.search(words[i - 1]["w"]):
                lines.append(f"[{first + offset}] {words[first]['s']:.1f} {' '.join(current)}")
                if gap >= SHOW_PAUSE_S:
                    lines.append(f"(пауза {gap:.1f} с)")
                current = []
        if not current:
            first = i
        current.append(word["w"])
    if current:
        lines.append(f"[{first + offset}] {words[first]['s']:.1f} {' '.join(current)}")
    return "\n".join(lines)


# ---------- правила и уроки ----------


def base_rules() -> str:
    return RULES.read_text(encoding="utf-8")


def lessons(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else LESSONS_TEMPLATE


def terms(text: str) -> list[str]:
    """Термины из раздела «## Термины» уроков: строки списка."""
    out, inside = [], False
    for line in text.splitlines():
        if line.startswith("## "):
            inside = line[3:].strip().lower() == "термины"
            continue
        if inside and line.lstrip().startswith(("- ", "* ")):
            term = line.lstrip()[2:].strip()
            if term:
                out.append(term)
    return out


def add_lesson(text: str, lesson: str, today: date | None = None) -> str:
    """Дописать правило в раздел «## Правила» (нет раздела — в конец)."""
    lesson = " ".join(lesson.split())
    if not lesson:
        return text
    line = f"- {(today or date.today()).isoformat()}: {lesson}"
    if "## Правила" not in text:
        return text.rstrip() + "\n\n## Правила\n\n" + line + "\n"
    return text.rstrip() + "\n" + line + "\n"


# ---------- промпты ----------

ANSWER_PLAN = """Ответь только JSON без пояснений:
{"cuts": [{"from": <номер первого слова>, "to": <номер последнего, включительно>,
           "kind": "<вид>", "reason": "<коротко, почему>"}],
 "layout": [{"from": <номер слова, с которого план>, "layout": "<план>"}],
 "notes": "<что не смог выполнить или на что посмотреть человеку; пусто, если нечего>"}
Виды вырезок: filler, retake, slip, command, extra. Планы: head, screen, split."""
ANSWER_REVISE = ANSWER_PLAN.replace('{"cuts"', '{"lesson": "<правило или пусто>",\n "cuts"', 1)


def _context(rules: str, learned: str, has_screen: bool) -> str:
    screen = (
        "Есть запись экрана: выбирай планы head, screen, split по правилам."
        if has_screen
        else "Записи экрана нет: план всегда head, поле layout — пустой список."
    )
    return f"{rules}\n\n{learned}\n\n{screen}"


def plan_prompt(packed: str, rules: str, learned: str, has_screen: bool, title: str = "") -> str:
    return (
        "### task: montage_plan\n"
        f"{_context(rules, learned, has_screen)}\n\n"
        f"Ролик: {title or 'без названия'}. Найди, что вырезать, и расставь планы.\n\n"
        f"{ANSWER_PLAN}\n\nРасшифровка:\n{packed}\n"
    )


def revise_prompt(
    packed: str, rules: str, learned: str, has_screen: bool, cuts: list[dict], layout: list[dict], feedback: str
) -> str:
    current = "\n".join(f"- {c['from']}–{c['to']} {c['kind']}: {c['reason']}" for c in cuts) or "- (нет вырезок)"
    plans = ", ".join(f"с {m['from']} — {m['layout']}" for m in layout)
    return (
        "### task: montage_revise\n"
        f"{_context(rules, learned, has_screen)}\n\n"
        f"Текущий план вырезок:\n{current}\nПланы: {plans}\n\n"
        f"Замечание человека к монтажу:\n{feedback.strip()}\n\n"
        "Переделай план целиком с учётом замечания. Отдельно сформулируй, какое общее правило "
        "из этого замечания стоит запомнить на будущие ролики (одна фраза; не про этот ролик, а про "
        "монтаж вообще; пусто, если замечание частное).\n"
        f"{ANSWER_REVISE}\n\n"
        f"Расшифровка:\n{packed}\n"
    )


def check_prompt(packed_output: str, rules: str, learned: str) -> str:
    return (
        "### task: montage_check\n"
        f"{rules}\n\n{learned}\n\n"
        "Перед тобой расшифровка уже смонтированного ролика. Найди, что монтаж пропустил: "
        "оставшиеся оговорки, дубли, паразиты, оборванные слова, команды монтажёру. Только явное — "
        "сомнительное не трогай. Нашёл мало или ничего — это хорошо.\n"
        'Ответь только JSON: {"issues": [{"from": <номер слова>, "to": <номер>, "kind": "<вид>", '
        '"reason": "<почему>"}]}\n\n'
        f"Расшифровка:\n{packed_output}\n"
    )


# ---------- разбор ответов ----------


def parse_plan(data: object, n_words: int, has_screen: bool) -> dict:
    if not isinstance(data, dict):
        raise ValueError("ИИ вернул не объект JSON.")
    return {
        "cuts": edl.clean_cuts(data.get("cuts"), n_words),
        "layout": edl.clean_layout(data.get("layout"), n_words, has_screen),
        "notes": str(data.get("notes") or "").strip()[:2000],
        "lesson": str(data.get("lesson") or "").strip()[:500],
    }


def parse_issues(data: object, n_words: int) -> list[dict]:
    issues = data.get("issues") if isinstance(data, dict) else None
    return edl.clean_cuts(issues, n_words, default_kind="check")
