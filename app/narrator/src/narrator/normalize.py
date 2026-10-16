"""Сырой текст → текст для озвучки: только кириллица и знаки препинания.

Модели озвучки не читают цифры, латиницу, сокращения и разметку: «42%»
превращается в «42 пациента», «API» — в «ПИ». Раньше текст по правилам
docs/tts-guide.md готовил человек или агент; normalize() делает это сам, чтобы
можно было вставить любой текст и послушать.

Порядок работы над строкой: разметка и ссылки → сокращения → словарь терминов
(lexicon.TERMS) → числа словами, в падеже по соседним словам → оставшаяся
латиница по словарю произношений CMUdict или по правилам чтения → символы.

Число строк сохраняется: номер строки блока в озвучке совпадает с номером
строки исходного текста. Текст, уже написанный по правилам, не меняется.
"""

from __future__ import annotations

import re
from functools import lru_cache

from narrator.lexicon import (
    ABBREVIATIONS,
    CYRILLIC_LETTERS,
    LATIN_LETTERS,
    MONTHS,
    RUSSIAN_ABBREVIATIONS,
    TERMS,
    UNITS,
    UNITS_GENITIVE,
)
from narrator.script import ACUTE, HEADING_PREFIX, MIN_BLOCK_WORDS
from narrator.script import normalize as normalize_marks

CYR = "а-яёА-ЯЁ"
VOWELS_RU = "аеёиоуыэюя"

# --- числа -----------------------------------------------------------------

# Предлог перед числом → падеж числительного (обозначения num2words).
CASE_AFTER = {
    **dict.fromkeys(
        "от до из без у около более менее больше меньше свыше после для порядка среди "
        "кроме вместо ранее позднее старше младше дороже дешевле быстрее медленнее".split(),
        "g",
    ),
    **dict.fromkeys(["к"], "d"),
    **dict.fromkeys(["о", "об", "при"], "p"),
}
ORDINAL_SUFFIX = {  # «1-й», «2-го», «90-х» → (падеж, род, множественное)
    **dict.fromkeys(["й", "ый", "ий", "ой"], ("n", "m", False)),
    **dict.fromkeys(["я", "ая"], ("n", "f", False)),
    **dict.fromkeys(["е", "ое"], ("n", "n", False)),
    **dict.fromkeys(["го", "ого"], ("g", "m", False)),
    **dict.fromkeys(["му", "ому"], ("d", "m", False)),
    **dict.fromkeys(["м", "ом"], ("p", "m", False)),
    **dict.fromkeys(["ю", "ую"], ("a", "f", False)),
    **dict.fromkeys(["х", "ых"], ("g", "m", True)),
    **dict.fromkeys(["ые"], ("n", "m", True)),
}
YEAR_FORMS = {  # слово после года → (падеж порядкового, как читать само слово)
    "год": ("n", "год"),
    "года": ("g", "года"),
    "году": ("p", "году"),
    "годом": ("i", "годом"),
    "годе": ("p", "годе"),
    "г": ("g", "года"),
}
FRACTIONS = {1: ("десятая", "десятых"), 2: ("сотая", "сотых"), 3: ("тысячная", "тысячных")}
ROMAN = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
CENTURY_FORMS = {  # слово после римского числа → (падеж, род)
    "век": ("n", "m"), "века": ("g", "m"), "веке": ("p", "m"), "веку": ("d", "m"),
    "веком": ("i", "m"), "квартал": ("n", "m"), "квартала": ("g", "m"),
    "квартале": ("p", "m"), "глава": ("n", "f"), "главе": ("p", "f"), "часть": ("n", "f"),
    "части": ("g", "f"), "том": ("n", "m"), "тома": ("g", "m"),
}  # fmt: skip

_NUM = r"\d+(?:[ \u00a0\u202f]\d{3})*"  # «10 000» — одно число


def _words(number: int, case: str = "n", gender: str = "m", ordinal: bool = False, plural=False):
    from num2words import num2words

    if number < 0:
        return "минус " + _words(-number, case, gender, ordinal, plural)
    kind = "ordinal" if ordinal else "cardinal"
    text = num2words(number, lang="ru", to=kind, case=case, gender=gender, plural=plural)
    # «тысяча двести», а не «одна тысяча двести».
    return re.sub(r"^(?:одна|одной|одну) (?=тысяч)", "", text)


def _digits(text: str) -> str:
    """Номер, код, число с ведущим нулём — по цифрам."""
    return " ".join(_words(int(d)) for d in text)


def _int(text: str) -> int:
    return int(re.sub(r"\D", "", text))


def _plural(number: int, one: str, few: str, many: str) -> str:
    n = abs(number) % 100
    if 11 <= n <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def _prev_word(text: str, pos: int) -> str:
    m = re.search(rf"([{CYR}]+)[\s«\"(]*(?:минус\s+)?$", text[:pos])
    return m.group(1).lower() if m else ""


def _next_word(text: str, pos: int) -> str:
    m = re.match(rf"[\s»\")]*([{CYR}]+)", text[pos:])
    return m.group(1).lower() if m else ""


def _cardinal(number: int, raw: str, text: str, start: int, end: int) -> str:
    """Количественное числительное: падеж — по предлогу слева, род — по слову справа."""
    if len(raw) > 1 and raw.startswith("0") or len(re.sub(r"\D", "", raw)) > 15:
        return _digits(re.sub(r"\D", "", raw))
    case = CASE_AFTER.get(_prev_word(text, start), "n")
    gender = "m"
    nxt = _next_word(text, end)
    last = abs(number) % 100
    if case == "n" and nxt and last not in (11, 12):
        if last % 10 == 2 and nxt[-1] in "ыи":  # «две недели», но «два дня»
            gender = "f"
        elif last % 10 == 1 and nxt[-1] in "ая":
            gender = "f"
        elif last % 10 == 1 and nxt[-1] in "ое" and len(nxt) > 3:
            gender = "n"
    return _words(number, case, gender)


def _with_unit(number: int, unit: str, case: str) -> str:
    one, few, many, gender = UNITS[unit]
    if case == "g" and one in UNITS_GENITIVE:
        singular, plural = UNITS_GENITIVE[one]
        word = singular if abs(number) % 10 == 1 and abs(number) % 100 != 11 else plural
        return f"{_words(number, 'g', gender)} {word}"
    return f"{_words(number, 'n', gender)} {_plural(number, one, few, many)}"


def _decimal(whole: str, frac: str) -> str:
    """«3,5» → «три с половиной», «0,25» → «ноль целых двадцать пять сотых»."""
    w = int(whole)
    if frac == "5" and w >= 1:
        return f"{_words(w)} с половиной"
    if len(frac) not in FRACTIONS:
        return f"{_words(w)} запятая {_digits(frac)}"
    f = int(frac)
    one, many = FRACTIONS[len(frac)]
    unit = one if f % 10 == 1 and f % 100 != 11 else many
    part = "целая" if w % 10 == 1 and w % 100 != 11 else "целых"
    return f"{_words(w, gender='f')} {part} {_words(f, gender='f')} {unit}"


def _roman(text: str) -> int | None:
    total = 0
    for a, b in zip(text, text[1:] + " ", strict=True):
        if a not in ROMAN:
            return None
        total += -ROMAN[a] if b in ROMAN and ROMAN[b] > ROMAN[a] else ROMAN[a]
    return total or None


_UNIT_KEYS = "|".join(
    sorted((re.escape(u) for u in UNITS if u not in "%$€₽"), key=len, reverse=True)
)
_MONTHS = "|".join(MONTHS)

_RE_ISO_DATE = re.compile(r"(?<![\d.])(\d{4})-(\d{2})-(\d{2})(?![\d-])")
_RE_DOT_DATE = re.compile(r"(?<![\d.])(\d{1,2})\.(\d{2})\.(\d{4})(?![\d.])")
_RE_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3]):([0-5]\d)(?![\d:])")
_RE_DAY_MONTH = re.compile(rf"(?<![\d.,])(\d{{1,2}})[ \u00a0]+({_MONTHS})(?![{CYR}])")
_RE_YEAR = re.compile(r"(?<![\d.,-])(\d{4})[ \u00a0]*(года|году|годом|годе|год|г\.)(?![а-яё])")
_RE_YEAR_BARE = re.compile(
    rf"(?<![{CYR}])([Вв]|[Сс]|[Кк]|до|от|после|из)[ \u00a0]+((?:19|20)\d\d)"
    rf"(?![.,]?\d|[%-]|[ \u00a0]*[{CYR}])"
)
_RE_ORDINAL = re.compile(
    rf"(?<![\d.,])(\d+)-(ого|ому|ый|ий|ой|ая|ое|ую|ых|ые|го|му|ом|й|я|е|м|ю|х)(?![{CYR}])"
)
_RE_COMPOUND = re.compile(rf"(?<![\d.,])(\d+)-([{CYR}]{{4,}})")
_RE_ROMAN = re.compile(rf"\b([IVXLC]+)[ \u00a0]+({'|'.join(CENTURY_FORMS)})(?![{CYR}])")
_RE_CURRENCY_BEFORE = re.compile(rf"([$€₽])[ \u00a0]?({_NUM})(?![\d,.]\d)")
_RE_DECIMAL = re.compile(
    rf"(?<![\d.,])(\d+),(\d+)(?![\d,])"
    rf"(?:[ \u00a0]?(?:([%$€₽])|({_UNIT_KEYS})\.?(?![{CYR}a-zA-Z])))?",
    re.I,
)
_RE_THOUSANDS = re.compile(rf"(?<=\d)[kKкК](?![{CYR}A-Za-z\d])")
_RE_TIMES = re.compile(r"(?<=\d)[ \u00a0]*[x×х][ \u00a0]*(?=\d)")
_RE_GLUED = re.compile(rf"(?<=[A-Za-z{CYR}])-(?=\d)")
_RE_VERSION = re.compile(r"(?<![\d.,])\d+(?:\.\d+)+")
_RE_UNIT = re.compile(
    rf"(?<![\d.,])({_NUM})[ \u00a0]?(?:([%$€₽])|({_UNIT_KEYS})\.?(?![{CYR}a-zA-Z]))", re.I
)
_RE_RANGE = re.compile(r"(?<=\d)[ \u00a0]?[–—-][ \u00a0]?(?=\d)")
_RE_NUMBER = re.compile(rf"(?<![\d])[-−]?{_NUM}")
_RE_NEGATIVE = re.compile(r"(?<![\w)])[-−](?=\d)")


def _roman_ordinal(m: re.Match) -> str:
    """«XXI век» → «двадцать первый век»."""
    number = _roman(m[1])
    if number is None:
        return m[0]
    case, gender = CENTURY_FORMS[m[2]]
    return f"{_words(number, case, gender, ordinal=True)} {m[2]}"


def _dates(text: str) -> str:
    def full(day: str, month: str, year: str) -> str:
        m = int(month)
        if not 1 <= m <= 12 or not 1 <= int(day) <= 31:
            return f"{_digits(day)} {_digits(month)} {_digits(year)}"
        return (
            f"{_words(int(day), 'n', 'n', ordinal=True)} {MONTHS[m - 1]} "
            f"{_words(int(year), 'g', 'm', ordinal=True)} года"
        )

    text = _RE_ISO_DATE.sub(lambda m: full(m[3], m[2], m[1]), text)
    text = _RE_DOT_DATE.sub(lambda m: full(m[1], m[2], m[3]), text)

    def day_month(m: re.Match) -> str:
        prev = _prev_word(text, m.start())
        case = "n" if prev in ("на", "за") else "d" if prev == "к" else "g"
        return f"{_words(int(m[1]), case, 'n', ordinal=True)} {m[2]}"

    text = _RE_DAY_MONTH.sub(day_month, text)

    def year(m: re.Match) -> str:
        word = m[2].rstrip(".")
        case, spoken = YEAR_FORMS[word]
        prev = _prev_word(text, m.start())
        if word == "г" and prev in ("в", "о", "при"):
            case, spoken = "p", "году"
        elif word == "году" and prev in ("к", "по"):
            case = "d"
        return f"{_words(int(m[1]), case, 'm', ordinal=True)} {spoken}"

    text = _RE_YEAR.sub(year, text)

    def bare_year(m: re.Match) -> str:
        case = {"в": "p", "к": "d"}.get(m[1].lower(), "g")
        return f"{m[1]} {_words(int(m[2]), case, 'm', ordinal=True)}"

    return _RE_YEAR_BARE.sub(bare_year, text)


def _numbers(text: str) -> str:
    """Все цифры строки — словами."""
    if not re.search(r"\d", text):
        return _RE_ROMAN.sub(_roman_ordinal, text)
    text = _RE_GLUED.sub(" ", text)  # «CVE-2026» → «CVE 2026»
    text = _dates(text)
    text = _RE_TIME.sub(
        lambda m: f"{_words(int(m[1]))} {_digits(m[2]) if m[2][0] == '0' else _words(int(m[2]))}",
        text,
    )

    text = _RE_ROMAN.sub(_roman_ordinal, text)

    def ordinal(m: re.Match) -> str:
        case, gender, plural = ORDINAL_SUFFIX[m[2]]
        nxt = _next_word(text, m.end())
        if m[2] == "е" and nxt.startswith("год"):  # «90-е годы»
            plural, gender = True, "m"
        return _words(int(m[1]), case, gender, ordinal=True, plural=plural)

    text = _RE_ORDINAL.sub(ordinal, text)

    def compound(m: re.Match) -> str:  # «5-летний» → «пятилетний»
        n = int(m[1])
        stem = {1: "одно", 90: "девяносто", 100: "сто", 1000: "тысяче"}.get(n)
        return (stem or _words(n, "g").replace(" ", "")) + m[2]

    text = _RE_COMPOUND.sub(compound, text)
    text = _RE_CURRENCY_BEFORE.sub(lambda m: f"{m[2]}{m[1]}", text)  # «$5» → «5$»
    text = _RE_THOUSANDS.sub(" тыс.", text)  # «10k» → «10 тыс.»
    text = _RE_TIMES.sub(" на ", text)  # «1920x1080»
    text = _RE_VERSION.sub(lambda m: " точка ".join(_words(int(p)) for p in m[0].split(".")), text)

    def decimal(m: re.Match) -> str:  # после дроби единица в родительном: «полтора процента»
        unit = (m[3] or m[4] or "").lower()
        return _decimal(m[1], m[2]) + (f" {UNITS[unit][1]}" if unit else "")

    text = _RE_DECIMAL.sub(decimal, text)

    def unit(m: re.Match) -> str:
        key = (m[2] or m[3]).lower()
        case = CASE_AFTER.get(_prev_word(text, m.start()), "n")
        return _with_unit(_int(m[1]), key, "g" if case == "g" else "n")

    text = _RE_UNIT.sub(unit, text)
    text = _RE_RANGE.sub(" — ", text)
    text = _RE_NEGATIVE.sub("минус ", text)

    out, pos = [], 0
    for m in _RE_NUMBER.finditer(text):
        raw = m[0].lstrip("-−")
        start = m.end() - len(raw)
        out.append(text[pos:start])
        out.append(_cardinal(_int(raw), raw, text, start, m.end()))
        pos = m.end()
    out.append(text[pos:])
    # Число вплотную к слову («икс5» → «икс пять») — разделяем пробелом.
    text = "".join(out)
    return re.sub(rf"(?<=[{CYR}])(?=\d)|(?<=\d)(?=[{CYR}])", " ", text)


# --- латиница ---------------------------------------------------------------

# Фонемы CMUdict (ARPAbet) → русские буквы. Гласные «е/э» уточняются по соседям.
ARPABET = {
    "AA": "а", "AE": "э", "AH": "а", "AO": "о", "AW": "ау", "AY": "ай", "EH": "э",
    "ER": "ер", "EY": "ей", "IH": "и", "IY": "и", "OW": "оу", "OY": "ой", "UH": "у",
    "UW": "у", "B": "б", "CH": "ч", "D": "д", "DH": "з", "F": "ф", "G": "г", "HH": "х",
    "JH": "дж", "K": "к", "L": "л", "M": "м", "N": "н", "NG": "нг", "P": "п", "R": "р",
    "S": "с", "SH": "ш", "T": "т", "TH": "т", "V": "в", "W": "в", "Y": "й", "Z": "з",
    "ZH": "ж",
}  # fmt: skip

# Правила чтения для слов, которых нет в CMUdict: (образец, замена), по порядку.
SPELLING_RULES: list[tuple[str, str]] = [
    (r"tion\b", "шн"), (r"sion\b", "жн"), (r"ture\b", "чер"), (r"ough", "о"),
    (r"eigh", "ей"), (r"igh", "ай"), (r"tch", "ч"), (r"sch", "ш"), (r"ch", "ч"),
    (r"sh", "ш"), (r"th", "т"), (r"ph", "ф"), (r"wh", "в"), (r"ck", "к"), (r"qu", "кв"),
    (r"kn", "н"), (r"wr", "р"), (r"ng\b", "нг"), (r"oo", "у"), (r"ee", "и"), (r"ea", "и"),
    (r"ou", "ау"), (r"ow\b", "оу"), (r"ow", "ау"), (r"ew", "ью"), (r"au", "о"),
    (r"aw", "о"), (r"ai", "ей"), (r"ay", "ей"), (r"oi", "ой"), (r"oy", "ой"),
    (r"ie\b", "и"), (r"\by", "й"), (r"y\b", "и"), (r"c(?=[eiy])", "с"), (r"c", "к"),
    (r"g(?=e\b)", "дж"), (r"j", "дж"), (r"x", "кс"), (r"q", "к"), (r"w", "в"),
    (r"z", "з"), (r"h", "х"), (r"a", "а"), (r"b", "б"), (r"d", "д"), (r"e", "е"),
    (r"f", "ф"), (r"g", "г"), (r"i", "и"), (r"k", "к"), (r"l", "л"), (r"m", "м"),
    (r"n", "н"), (r"o", "о"), (r"p", "п"), (r"r", "р"), (r"s", "с"), (r"t", "т"),
    (r"u", "у"), (r"v", "в"), (r"y", "и"),
]  # fmt: skip
_REDUCED = {"a": "а", "e": "е", "i": "и", "o": "о", "u": "а", "y": "и"}
_MAGIC_E = {"a": "ей", "i": "ай", "o": "оу", "u": "ью"}


@lru_cache(maxsize=1)
def _cmudict() -> dict[str, list[list[str]]]:
    import cmudict

    return cmudict.dict()


def _from_phonemes(phones: list[str], word: str) -> str:
    """Транскрипция CMUdict → кириллица с ударением на главном ударном слоге."""
    out: list[str] = []
    syllables = sum(p[-1].isdigit() for p in phones)
    aa = "о" if "o" in word and "a" not in word else "а"  # «doc» → «док», «car» → «кар»
    # Безударный нейтральный гласный в русском пишут по английской букве:
    # «open» → «оупен», «telemetry» → «телеметри». Буквы берём по порядку слогов.
    groups = re.findall(r"[aeiouy]+", word)
    if len(groups) == syllables + 1 and word.endswith("e"):
        groups.pop()  # немая «e»
    spelled = groups if len(groups) == syllables else []
    vowel_no = -1
    skip = False
    for i, raw in enumerate(phones):
        if skip:
            skip = False
            continue
        stress = raw[-1] if raw[-1].isdigit() else ""
        ph = raw.rstrip("012")
        vowel_no += bool(stress)
        reduced = _REDUCED.get(spelled[vowel_no][0]) if stress == "0" and spelled else None
        nxt = phones[i + 1].rstrip("012") if i + 1 < len(phones) else ""
        prev_is_consonant = bool(out) and out[-1][-1] not in VOWELS_RU + ACUTE
        if ph == "Y" and nxt in ("UW", "UH"):  # «ю» вместо «йу»
            letters, skip = "ю", True
            stress = phones[i + 1][-1]
        elif ph == "AH" and reduced:
            letters = reduced
        elif ph == "ER" and reduced:
            letters = reduced + "р"
        elif ph == "AA":
            letters = aa
        elif ph == "EH":
            letters = "е" if prev_is_consonant else "э"
        elif ph == "EY":
            letters = "ей" if prev_is_consonant else "эй"
        elif ph == "ER" and stress == "1" and prev_is_consonant:
            letters = "ёр"
        else:
            letters = ARPABET[ph]
        if stress == "1" and syllables > 1 and letters[0] != "ё":
            letters = letters[0] + ACUTE + letters[1:]
        out.append(letters)
    return re.sub(r"(.)\1", r"\1", "".join(out))  # сдвоенные буквы — одной


def _by_rules(word: str) -> str:
    """Чтение по буквам и буквосочетаниям — для слов вне словаря."""
    w = word.lower()
    m = re.fullmatch(r"(.*[^aeiou])([aiou])([^aeiouwxy])e", w)  # «base» → «бейс»
    tail = ""
    if m:
        w, tail = m[1], _MAGIC_E[m[2]] + _by_rules(m[3])
    elif len(w) > 3 and w.endswith("e") and w[-2] not in "aeiou":
        w = w[:-1]  # немая «e»
    out, i = [], 0
    while i < len(w):
        for pattern, repl in SPELLING_RULES:
            m = re.compile(pattern).match(w, i)
            if m:
                out.append(repl)
                i = m.end()
                break
        else:
            i += 1  # апостроф и прочее
    text = "".join(out) + tail
    text = re.sub(r"^е", "э", text)
    return re.sub(r"(.)\1", r"\1", text)


def _spell(word: str) -> str:
    return " ".join(LATIN_LETTERS[c] for c in word.lower() if c in LATIN_LETTERS)


@lru_cache(maxsize=4096)
def _latin_word(word: str) -> str:
    """Одно латинское слово кириллицей."""
    low = word.lower().replace("’", "'")
    if low in TERMS:
        return TERMS[low]
    letters = re.sub(r"[^a-z]", "", low)
    if not letters:
        return ""
    known = _cmudict().get(low) or _cmudict().get(letters)
    if word.isupper() and (len(letters) <= 3 or not known):
        return _spell(letters)  # аббревиатура: «SRE» → «эс ар и»
    if len(letters) == 1:
        return _spell(letters)
    if known:
        return _from_phonemes(known[0], letters)
    if not re.search(r"[aeiouy]", letters):
        return _spell(letters)  # без гласных не произнести: «gRPC», «kb»
    return _by_rules(letters)


_RE_CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+(?:'[a-z]+)?|[A-Z]+")
_RE_LATIN = re.compile(r"[A-Za-z][A-Za-z'’]*")


def _latin_token(token: str) -> str:
    """Слово или склейка слов: «DeepSeek» → «дип сик»."""
    low = token.lower().replace("’", "'")
    if low in TERMS:
        return TERMS[low]
    if low in _cmudict() and not (token.isupper() and len(token) <= 3):
        return _latin_word(token)
    parts = _RE_CAMEL.findall(token.replace("’", "'")) or [token]
    return " ".join(filter(None, (_latin_word(p) for p in parts)))


def _terms_pattern() -> re.Pattern[str]:
    keys = sorted(TERMS, key=len, reverse=True)
    body = "|".join(re.escape(k).replace(r"\ ", r"[ \u00a0]+") for k in keys)
    # Слева и справа — не буква и не цифра: «go» внутри «google» не трогаем.
    return re.compile(rf"(?<![A-Za-z0-9{CYR}])(?:{body})(?![A-Za-z0-9{CYR}])", re.I)


_RE_TERMS = _terms_pattern()
_RE_RUS_ABBR = re.compile(rf"(?<![{CYR}])({'|'.join(RUSSIAN_ABBREVIATIONS)})(?![{CYR}])")
_RE_DIGIT_LETTERS = re.compile(r"(?<![\d.,A-Za-z])(\d+)([A-Z]{1,2})(?![A-Za-z\d])")
_RE_ALNUM = re.compile(
    rf"(?<![A-Za-z{CYR}\d])([A-Za-z{CYR}]{{1,3}})(\d+)([A-Za-z]{{0,2}})(?![A-Za-z{CYR}\d])"
)


def _terms(text: str) -> str:
    def term(m: re.Match) -> str:
        key = re.sub(r"[ \u00a0]+", " ", m[0].lower()).replace("’", "'")
        return _keep_case(m[0], TERMS[key]) if key in TERMS else m[0]

    text = _RE_TERMS.sub(term, text.replace("’", "'"))
    text = _RE_RUS_ABBR.sub(lambda m: RUSSIAN_ABBREVIATIONS[m[1]], text)

    def alnum(m: re.Match) -> str:  # «GPT4», «h264», «v2» — буквы по названиям, число отдельно
        head, digits, tail = m[1], m[2], m[3]
        if re.fullmatch(rf"[{CYR}]+", head) and not head.isupper():
            return m[0]  # «в2» — обычный предлог со слипшимся числом
        if head.lower() == "v":
            spoken = "версия"
        elif re.fullmatch(r"[A-Za-z]+", head):
            spoken = _latin_token(head) if len(head) > 2 and not head.isupper() else _spell(head)
        else:
            spoken = " ".join(CYRILLIC_LETTERS[c] for c in head.lower())
        return f"{spoken} {digits} {_spell(tail)}".strip()

    text = _RE_ALNUM.sub(alnum, text)

    def digit_letters(m: re.Match) -> str:  # «3D», «5G»; «5GB» — единица, её читают числа
        return m[0] if m[2].lower() in UNITS else f"{m[1]} {_spell(m[2])}"

    return _RE_DIGIT_LETTERS.sub(digit_letters, text)


def _latin(text: str) -> str:
    return _RE_LATIN.sub(lambda m: _keep_case(m[0], _latin_token(m[0])), text)


# --- разметка и символы -----------------------------------------------------

# Адрес без знака препинания в конце: точка после ссылки завершает предложение.
_URL = r"(?:https?://|www\.)[^\s<>]*[^\s<>.,;:!?)\]»\"']"
_MARKDOWN = [
    (re.compile(r"!\[[^\]]*\]\([^)]*\)"), ""),  # картинка
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),  # ссылка → её текст
    # «см. https://…» — без адреса отсылка повисает, убираем вместе с ним
    (re.compile(rf"(?:,\s*)?(?:см\.|смотрите?|подробнее)[:\s]*<?{_URL}>?", re.I), ""),
    (re.compile(r"<https?://[^>]+>"), ""),
    (re.compile(_URL), ""),
    (re.compile(r"\S+@\S+\.\w+"), ""),  # адрес почты
    (re.compile(r"<[^>]+>"), ""),  # теги
    (re.compile(r"`+"), ""),
    (re.compile(r"(\*\*|__|~~)"), ""),
    (re.compile(r"(?<!\w)[*_](?=\S)|(?<=\S)[*_](?!\w)"), ""),
    (re.compile(r"^\s*(?:[-*•·]|\d+[.)])\s+"), ""),  # маркер списка
    (re.compile(r"^\s*>\s?"), ""),  # цитата
    (re.compile(r"\[\d+\]"), ""),  # сноска
]
_SYMBOLS = [
    (re.compile(r"\s*&\s*"), " и "),
    (re.compile(r"\s*\+\s*"), " плюс "),
    (re.compile(r"\s*=\s*"), " равно "),
    (re.compile(r"\s*[≈~]\s*(?=\d)"), " примерно "),
    (re.compile(r"\s*→\s*"), ", "),
    (re.compile(r"№\s*"), "номер "),
    (re.compile(r"°\s*[CС]"), " градусов"),
    (re.compile(r"°"), " градусов"),
    (re.compile(r"\s*/\s*"), " "),
    (re.compile(r"\s*[(\[{]\s*"), ", "),
    (re.compile(r"\s*[)\]}]"), ","),
    (re.compile(r"[“”„]"), '"'),
    (re.compile(r"[‘’`]"), "'"),
    (re.compile(r"\.{3,}"), "…"),
]
_NOT_ALLOWED = re.compile(r"[^а-яёА-ЯЁ\s.,!?;:\-—–«»\"'…" + ACUTE + r"]")


def _tidy(text: str) -> str:
    """Убирает то, что осталось от вырезанных символов: лишние пробелы и запятые."""
    text = _NOT_ALLOWED.sub(" ", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\s+([,.!?;:…])", r"\1", text)
    text = re.sub(r",(?:\s*,)+", ",", text)
    text = re.sub(r",\s*([.!?;:…])", r"\1", text)
    text = re.sub(r"([.!?…])\s*,", r"\1", text)
    text = re.sub(r"(?<![а-яёА-ЯЁ])'|'(?![а-яёА-ЯЁ])", "", text)  # одиночный апостроф
    text = re.sub(r"([!?…])\.", r"\1", text)
    text = re.sub(r"^[\s,;:.]+(?!\.)", "", text)
    return text.strip()


def _keep_case(source: str, spoken: str) -> str:
    """«GitHub» → «Гитхаб», «Go» → «Го»; аббревиатуры («API») остаются строчными."""
    proper = source[:1].isupper() and not source.isupper()
    return spoken[:1].upper() + spoken[1:] if proper else spoken


def speakable(line: str) -> str:
    """Одна строка текста → только то, что модель может прочитать."""
    text = line
    for pattern, repl in _MARKDOWN:
        text = pattern.sub(repl, text)
    for pattern, repl in ABBREVIATIONS:
        text = re.sub(rf"(?<![{CYR}]){pattern}", repl, text, flags=re.I)
    text = _terms(text)
    text = _numbers(text)
    text = _latin(text)
    for pattern, repl in _SYMBOLS:
        text = pattern.sub(repl, text)
    text = _tidy(text)
    if re.match(r"\W*\d", line):  # строка начиналась с числа — теперь со слова
        text = text[:1].upper() + text[1:]
    return text


def normalize(raw: str) -> str:
    """Любой текст → текст для озвучки в формате script.txt, строка в строку."""
    lines = normalize_marks(raw).splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        heading = re.match(r"#{1,6}\s+", stripped)
        if heading:
            body = speakable(stripped[heading.end() :].rstrip("# "))
            out.append(HEADING_PREFIX + body if body else "")
        elif re.fullmatch(r"[-*_=]{3,}", stripped):  # линия-разделитель
            out.append("")
        else:
            out.append(speakable(stripped))
    return "\n".join(_merge_short(out)) + ("\n" if raw.endswith("\n") else "")


def _merge_short(lines: list[str]) -> list[str]:
    """Блок из одного-двух слов модель коверкает — присоединяем его к соседнему.

    Число строк не меняется: строки короткого блока пустеют, а его текст
    уходит отдельным предложением в начало следующего блока (у последнего
    блока — в конец предыдущего).
    """
    lines = list(lines)
    blocks: list[tuple[int, int]] = []  # [начало, конец) каждого блока
    start = None
    for i, line in enumerate([*lines, ""]):
        if line.strip() and start is None:
            start = i
        elif not line.strip() and start is not None:
            blocks.append((start, i))
            start = None
    for n, (a, b) in enumerate(blocks):
        body = " ".join(x.strip() for x in lines[a:b]).removeprefix(HEADING_PREFIX.strip()).strip()
        if not 0 < len(body.split()) < MIN_BLOCK_WORDS or len(blocks) == 1:
            continue
        sentence = body if body[-1] in ".!?…" else body + "."
        if n + 1 < len(blocks):
            j = blocks[n + 1][0]
            mark = HEADING_PREFIX if lines[j].startswith(HEADING_PREFIX) else ""
            lines[j] = f"{mark}{sentence} {lines[j].removeprefix(mark).strip()}"
        else:
            j = blocks[n - 1][1] - 1
            lines[j] = f"{lines[j].rstrip()} {sentence}"
        lines[a:b] = [""] * (b - a)
    return lines


def changed(raw: str, normalized: str) -> int:
    """Сколько строк нормализация изменила — для подсказки в интерфейсе."""
    pairs = zip(normalize_marks(raw).splitlines(), normalized.splitlines(), strict=False)
    return sum(a.strip() != b.strip() for a, b in pairs)
