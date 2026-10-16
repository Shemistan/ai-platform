"""Текст для озвучки (script.txt): разбор на блоки и проверка правил.

Формат script.txt: абзацы через пустую строку. Абзац, начинающийся с «# », —
заголовок раздела: он озвучивается, а вокруг него пауза длиннее. Правила
записи текста — docs/tts-guide.md; lint ловит то, что Supertonic 3 гарантированно
читает плохо (проверено распознаванием озвучки, см. спеку).
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

HEADING_PREFIX = "# "

# Ударение — комбинирующий акут после ударной гласной: «зáмок». Знак «+»
# модель озвучивает как призвук и ломает слово («з+амок» → «бламок»).
ACUTE = "\N{COMBINING ACUTE ACCENT}"

ALLOWED_CHARS = re.compile(r"[а-яёА-ЯЁ\s.,!?;:\-—–«»\"'…" + ACUTE + r"]")

MAX_SENTENCE = 250
# Одно-два слова отдельным клипом модель коверкает («Нейросети» → «Не Россия»),
# в составе фразы то же слово звучит чисто.
MIN_BLOCK_WORDS = 3

# Готовые «á», «é», «ó» набираются проще, чем акут, но это латиница: её
# гласную меняем на кириллическую двойницу, акут оставляем.
_LATIN_STRESSED = {
    "a": "а",
    "e": "е",
    "o": "о",
    "y": "у",
    "i": "и",
    "A": "А",
    "E": "Е",
    "O": "О",
    "Y": "У",
    "I": "И",
}


def normalize(text: str) -> str:
    """Приводит ударения к виду «кириллическая гласная + U+0301», прочее — к NFC."""
    decomposed = unicodedata.normalize("NFD", text)
    out: list[str] = []
    for i, ch in enumerate(decomposed):
        nxt = decomposed[i + 1] if i + 1 < len(decomposed) else ""
        out.append(_LATIN_STRESSED.get(ch, ch) if nxt == ACUTE else ch)
    # NFC собирает обратно «й» и «ё»; у кириллицы с акутом составных форм нет,
    # поэтому акут остаётся отдельным знаком.
    return unicodedata.normalize("NFC", "".join(out))


@dataclass(frozen=True)
class Block:
    kind: str  # "heading" | "para"
    text: str
    line: int  # номер первой строки блока в script.txt, с 1


@dataclass(frozen=True)
class Issue:
    line: int
    col: int
    level: str  # "error" | "warning"
    code: str
    message: str

    def __str__(self) -> str:
        return f"{self.line}:{self.col} {self.level} {self.code}: {self.message}"


def parse(text: str) -> list[Block]:
    """Делит script.txt на блоки по пустым строкам; строки абзаца склеивает пробелом."""
    text = normalize(text)
    blocks: list[Block] = []
    buf: list[str] = []
    start = 0
    for no, raw in enumerate(text.splitlines() + [""], start=1):
        line = raw.strip()
        if line:
            if not buf:
                start = no
            buf.append(line)
            continue
        if buf:
            joined = " ".join(buf)
            if joined.startswith(HEADING_PREFIX):
                blocks.append(Block("heading", joined[len(HEADING_PREFIX) :].strip(), start))
            else:
                blocks.append(Block("para", joined, start))
            buf = []
    return blocks


_RULES_ERROR: list[tuple[str, re.Pattern[str], str]] = [
    ("DIGIT", re.compile(r"\d+"), "цифры — словами: «сорок два процента», не «42%»"),
    (
        "LATIN",
        re.compile(r"[A-Za-z][A-Za-z.'\-]*"),
        "латиница — кириллицей, как произносят: «Кубернетес», «эй пи ай»",
    ),
    ("PLUS", re.compile(r"\+"), "«+» ломает слово; ударение — акутом после гласной: «зáмок»"),
    (
        "ABBR",
        re.compile(
            r"(?<![а-яёА-ЯЁ])(?:т\.\s?[епдк]|и\s?др|и\s?пр|др|см|стр|рис|табл|напр|гг?|вв?"
            r"|руб|коп|долл|мин|сек|ч)\.|(?<![а-яёА-ЯЁ])(?:млн|млрд|тыс|трлн)(?![а-яёА-ЯЁ])"
        ),
        "сокращение — полностью: «то есть», «миллионов», «тысяч»",
    ),
]

_RULES_WARNING: list[tuple[str, re.Pattern[str], str]] = [
    (
        "HYPHEN_CHAIN",
        re.compile(r"[а-яёА-ЯЁ]+(?:-[а-яёА-ЯЁ]+){2,}"),
        "цепочку через дефис модель смазывает — пиши раздельно: «Опен Эй Ай», «эй пи ай»",
    ),
    (
        "CAPS",
        re.compile(r"(?<![а-яёА-ЯЁ])[А-ЯЁ]{2,}(?![а-яёА-ЯЁ])"),
        "аббревиатуру капсом читает непредсказуемо — распиши по буквам или словами",
    ),
    (
        "STRESS_TWICE",
        re.compile(r"[а-яёА-ЯЁ]*" + ACUTE + r"[а-яёА-ЯЁ]*" + ACUTE),
        "два ударения в одном слове",
    ),
]

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def lint(text: str) -> list[Issue]:
    """Проверяет script.txt. Ошибки блокируют синтез, предупреждения — повод перечитать."""
    text = normalize(text)
    issues: list[Issue] = []
    lines = text.splitlines()
    for no, raw in enumerate(lines, start=1):
        offset = 0
        body = raw
        stripped = raw.lstrip()
        if stripped.startswith(HEADING_PREFIX):
            offset = len(raw) - len(stripped) + len(HEADING_PREFIX)
            body = raw[offset:]
        covered: set[int] = set()
        for rules, level in ((_RULES_ERROR, "error"), (_RULES_WARNING, "warning")):
            for code, pattern, message in rules:
                for m in pattern.finditer(body):
                    issues.append(
                        Issue(no, offset + m.start() + 1, level, code, f"«{m.group()}» — {message}")
                    )
                    if level == "error":
                        covered.update(range(m.start(), m.end()))
        for i, ch in enumerate(body):
            if i not in covered and not ALLOWED_CHARS.fullmatch(ch):
                issues.append(
                    Issue(no, offset + i + 1, "error", "CHAR", f"символ {ch!r} озвучивать нельзя")
                )
    for block in parse(text):
        if len(block.text.split()) < MIN_BLOCK_WORDS:
            issues.append(
                Issue(
                    block.line,
                    1,
                    "warning",
                    "SHORT_BLOCK",
                    f"«{block.text}» — короткий блок модель коверкает; сделай фразой: "
                    "«Часть третья. Нейросети.», «Переходим к нейросетям.»",
                )
            )
        for sentence in _SENTENCE_END.split(block.text):
            if len(sentence) > MAX_SENTENCE:
                issues.append(
                    Issue(
                        block.line,
                        1,
                        "warning",
                        "LONG_SENTENCE",
                        f"предложение {len(sentence)} символов «{sentence[:40]}…» — раздели",
                    )
                )
    if not parse(text):
        issues.append(Issue(1, 1, "error", "EMPTY", "текст для озвучки пуст"))
    issues.sort(key=lambda x: (x.line, x.col))
    return issues


def has_errors(issues: list[Issue]) -> bool:
    return any(i.level == "error" for i in issues)
