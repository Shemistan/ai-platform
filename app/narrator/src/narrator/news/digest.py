"""Оценка важности, события и дайджест.

Агент присылает факторы важности каждого материала, код считает балл по
формуле из docs/news/sources-v1.md (§13), применяет правило подтверждения
(§9), склеивает материалы в события и отбирает события в дайджест.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field

# Версия 2 (октябрь 2026): вес источника и «про наш стек» давали поставщику
# 0,40 балла заранее, и changelog GitHub и ClickHouse забивал главное. Теперь
# у разбора, из которого слушатель уносит знание, свой фактор insight.
WEIGHTS = {
    "global_impact": 0.15,
    "stack_relevance": 0.20,
    "novelty": 0.15,
    "authority": 0.10,
    "production_impact": 0.15,
    "insight": 0.15,
    "community_signal": 0.10,
}
SCORING_VERSION = "2"
# insight у оценок до версии 2 — по типу материала.
LEGACY_INSIGHT = {
    "engineering_deep_dive": 0.6,
    "architecture": 0.6,
    "postmortem": 0.7,
    "incident": 0.5,
    "benchmark": 0.5,
    "ai_research": 0.5,
    "conference_talk": 0.5,
    "tutorial": 0.4,
    "security": 0.4,
    "breaking_change": 0.4,
}
LEGACY_INSIGHT_DEFAULT = 0.2
FACTORS = tuple(k for k in WEIGHTS if k != "authority")  # authority — из конфига источника
KINDS = (
    "release",
    "security",
    "breaking_change",
    "deprecation",
    "new_feature",
    "incident",
    "postmortem",
    "engineering_deep_dive",
    "benchmark",
    "architecture",
    "ai_model",
    "ai_agent",
    "ai_tool",
    "ai_research",
    "video",
    "conference_talk",
    "tutorial",
    "community_signal",
    "discussion",
    "rumor",
)
# Важность 1…10: 1 — знать не нужно, 10 — критически важно знать всем.
SCALE = 10
IMPORTANT = 7  # от этой важности событие идёт в дайджест и обзор недели
NOTABLE = 5  # 5–6: добирают выпуск, если важных мало
UNCONFIRMED_CAP = 6  # вторичный источник без подтверждения — не выше, то есть не «важное»
# Мелкое изменение продукта: настройка, лимит, превью для enterprise — не выше 6.
MINOR_KINDS = ("new_feature", "release", "ai_tool")
MINOR_CAP = 6
MINOR_PRODUCTION = 0.5  # production_impact ниже этого…
MINOR_GLOBAL = 0.7  # …и global_impact ниже этого — изменение мелкое
# Главное выпуска: от одного поставщика не больше VENDOR_MAX событий. Поставщик,
# который был в главном за последние VENDOR_RECENT_DAYS дней, идёт с важностью
# на единицу ниже. Обязательные события (must_know) эти правила не трогают.
VENDOR_MAX = 2
VENDOR_RECENT_DAYS = 2
URGENT_KINDS = ("security", "breaking_change")
# Обязательное: уязвимость, breaking change, обязательная миграция (депрекейт с
# production от MIGRATION_PRODUCTION), массовый инцидент (global от MASS_GLOBAL),
# любое событие от MUST_LEVEL. Входит без лимита.
MUST_LEVEL = 9
MIGRATION_PRODUCTION = 0.8
MASS_GLOBAL = 0.7
# Разбор (рубрика «Инженерные блоги») от BLOG_MIN и один материал радара — только
# если есть место в бюджете длительности.
BLOG_MIN = 6
RADAR_TOP = 1
# Числа событий в выпуске нет: все важные входят. Тихий день (важных меньше
# DIGEST_MIN) добирается событиями 5–6. Ограничитель — длительность.
DIGEST_MIN = 3
WEEKLY_MIN = 5  # обзор недели: все события от IMPORTANT; меньше пяти — добрать NOTABLE
# Темп речи, слов в минуту: и озвучка дайджеста (Gemini TTS), и человек в кадре —
# обычная речь.
WORDS_PER_MIN_SPEAKER = 130
TELEPROMPTER_MAX_WORDS = 30  # фраза длиннее не читается с суфлёра на одном дыхании
MONTHLY_BUZZ = 15  # обзор месяца: громких событий индустрии не больше
BUZZ_SIGNAL = 0.7  # громкое — community_signal или global_impact от этого
MONTHLY_SECTIONS = ("Важное для разработчика", "О чём говорила индустрия")
DIGEST_MAX_MINUTES = 20
DIGEST_MAX_WORDS = DIGEST_MAX_MINUTES * WORDS_PER_MIN_SPEAKER
# Цель — 8–12 минут; выше — менее значимые темы коротко, без необязательных рубрик.
DIGEST_TARGET_MINUTES = (8, 12)
DIGEST_TARGET_WORDS = DIGEST_TARGET_MINUTES[1] * WORDS_PER_MIN_SPEAKER
# Оценка длины пересказа до его написания, в словах.
FRAME_WORDS = 120  # шапка, анонс рубрик, переходы, финал
FULL_WORDS = 200  # важная тема подробно: 2–3 абзаца
BRIEF_WORDS = 60  # тема коротко: 2–3 предложения
EXTRA_WORDS = 70  # YouTube, телеграм, разбор, радар: 2–4 предложения
# Пересказ на слух: аббревиатур, версий и путей — не больше стольких на сто слов.
# Выше — после минуты прослушивания хочется выключить.
JARGON_PER_100 = 2.0
POST_LIMIT = 900  # подпись к фото — 1024 знака, остальное под строку про аудио

_EVENT = re.compile(r"[a-z0-9][a-z0-9._-]*")


def score(factors: dict[str, float], authority: float) -> float:
    total = WEIGHTS["authority"] * authority
    total += sum(WEIGHTS[k] * factors[k] for k in FACTORS)
    return round(total, 4)


def importance(value: float) -> int:
    """Балл 0…1 → важность 1…10: 0–0.1 → 1, 0.1–0.2 → 2, …, 0.9–1 → 10."""
    return max(1, min(SCALE, int(round(value * SCALE, 6)) + 1))


def final_importance(
    value: float,
    secondary: bool,
    confirmed_by: str | None,
    kind: str = "",
    factors: dict[str, float] | None = None,
    ceiling: int = SCALE,
) -> int:
    level = min(importance(value), ceiling)
    if secondary and not confirmed_by:
        level = min(level, UNCONFIRMED_CAP)
    if kind in MINOR_KINDS and factors and is_minor(factors):
        level = min(level, MINOR_CAP)
    return level


def is_minor(factors: dict[str, float]) -> bool:
    return (
        factors.get("production_impact", 0.0) < MINOR_PRODUCTION
        and factors.get("global_impact", 0.0) < MINOR_GLOBAL
    )


def with_insight(factors: dict[str, float], kind: str) -> dict[str, float]:
    """Факторы оценки до версии 2: insight по типу материала."""
    if "insight" in factors:
        return factors
    return {**factors, "insight": LEGACY_INSIGHT.get(kind, LEGACY_INSIGHT_DEFAULT)}


@dataclass
class Assessment:
    id: str
    kind: str
    event: str
    title: str  # по-русски, одной строкой: что произошло
    why: str  # почему это важно Go/backend-разработчику
    factors: dict[str, float]
    confirmed_by: str | None = None


def parse_assessments(data: dict) -> tuple[list[Assessment], list[str], list[str]]:
    """JSON агента → (оценки, id пропущенных, ошибки).

    Формат: {"items": [{"id", "kind", "event", "title", "why", <факторы>,
    "confirmed_by"?}], "skip": [id, …]}.
    """
    out: list[Assessment] = []
    errors: list[str] = []
    for n, raw in enumerate(data.get("items", []), start=1):
        where = f"items[{n}] {raw.get('id', '?')}"
        problems = []
        if raw.get("kind") not in KINDS:
            problems.append(f"kind={raw.get('kind')!r} не из списка")
        event = str(raw.get("event", ""))
        if not _EVENT.fullmatch(event):
            problems.append("event — латиница, цифры, «.-_», например go-1-25-2")
        for key in ("id", "title", "why"):
            if not str(raw.get(key, "")).strip():
                problems.append(f"пустое {key}")
        factors = {}
        for key in FACTORS:
            value = raw.get(key)
            if not isinstance(value, int | float) or not 0 <= value <= 1:
                problems.append(f"{key} — число от 0 до 1")
            else:
                factors[key] = float(value)
        if problems:
            errors.append(f"{where}: " + "; ".join(problems))
            continue
        out.append(
            Assessment(
                id=raw["id"],
                kind=raw["kind"],
                event=event,
                title=raw["title"].strip(),
                why=raw["why"].strip(),
                factors=factors,
                confirmed_by=(raw.get("confirmed_by") or None),
            )
        )
    return out, [str(x) for x in data.get("skip", [])], errors


@dataclass
class Event:
    key: str
    score: float
    importance: int
    items: list[dict] = field(default_factory=list)
    main: bool = False  # главное выпуска, а не добор рубрик
    brief: bool = False  # главное, но коротко: 2–3 предложения

    @property
    def lead(self) -> dict:
        """Главный материал события: самый весомый."""
        return max(self.items, key=lambda i: (i["importance"], i["score"]))

    @property
    def sources(self) -> int:
        """Сколько разных источников написали о событии."""
        return len({i["source"] for i in self.items})


def group_events(rows: list[dict]) -> list[Event]:
    """Материалы с одним event → событие; по убыванию важности и балла."""
    events: dict[str, Event] = {}
    for row in rows:
        ev = events.setdefault(row["event"], Event(row["event"], 0.0, 0))
        ev.items.append(row)
        ev.score = max(ev.score, row["score"])
        ev.importance = max(ev.importance, row["importance"])
    return sorted(events.values(), key=lambda e: (-e.importance, -e.score, -e.sources, e.key))


def select(
    events: list[Event],
    minimum: int = DIGEST_MIN,
    level: Callable[[Event], int] | None = None,
    top: int | None = None,
) -> list[Event]:
    """Главное выпуска: все важные (от 7); меньше minimum — добрать 5–6.

    level — важность для отбора (по умолчанию своя у события), events — уже
    по её убыванию. top — только для ручного запуска с --top: обязательные
    события он не отрезает.
    """
    level = level or (lambda e: e.importance)
    strong = [e for e in events if level(e) >= IMPORTANT]
    if top is not None:
        must = [e for e in strong if must_know(e)]
        rest = [e for e in strong if not must_know(e)][: max(0, top - len(must))]
        strong = [e for e in strong if e in must or e in rest]
    if len(strong) >= minimum:
        return strong
    extra = [e for e in events if NOTABLE <= level(e) < IMPORTANT]
    return strong + extra[: minimum - len(strong)]


def must_know(ev: Event) -> bool:
    """Обязательное событие: входит в выпуск без лимита и правил поставщика."""
    if ev.importance < IMPORTANT:
        return False
    kind = ev.lead.get("kind")
    factors = json.loads(ev.lead.get("factors") or "{}")
    return (
        ev.importance >= MUST_LEVEL
        or kind in URGENT_KINDS
        or (kind == "deprecation" and factors.get("production_impact", 0) >= MIGRATION_PRODUCTION)
        or (kind == "incident" and factors.get("global_impact", 0) >= MASS_GLOBAL)
    )


def vendor_of(ev: Event, sources: dict) -> str:
    src = sources.get(ev.lead["source"])
    return src.vendor_key if src else ev.lead["source"]


def diversify(
    events: list[Event], sources: dict, recent: Collection[str] = ()
) -> tuple[list[Event], Callable[[Event], int]]:
    """Кандидаты в главное с поправкой на поставщика и важность для отбора.

    Поставщик из recent (был в главном в прошлые дни) идёт с важностью на
    единицу ниже; от одного поставщика — не больше VENDOR_MAX событий.
    Обязательные события (must_know) идут как есть и в лимит не считаются.
    """
    levels = {}
    for ev in events:
        penalty = 0 if must_know(ev) or vendor_of(ev, sources) not in recent else 1
        levels[ev.key] = ev.importance - penalty
    ranked = sorted(events, key=lambda e: (-levels[e.key], -e.score, -e.sources, e.key))
    out: list[Event] = []
    taken: dict[str, int] = {}
    for ev in ranked:
        if must_know(ev):
            out.append(ev)
            continue
        vendor = vendor_of(ev, sources)
        if taken.get(vendor, 0) >= VENDOR_MAX:
            continue
        taken[vendor] = taken.get(vendor, 0) + 1
        out.append(ev)
    return out, lambda e: levels[e.key]


# Разделы выпуска — как рубрики в новостях. Раздел события — по типу его главного
# источника; порядок здесь — порядок в выпуске.
SECTIONS = (
    ("releases", "Релизы и официальные новости", ("official", "official_changelog")),
    ("blogs", "Инженерные блоги", ("engineering_blog", "expert_blog")),
    ("discuss", "О чём спорят в сообществе", ("aggregator", "newsletter")),
    ("youtube", "YouTube и конференции", ("youtube_expert", "conference")),
    ("telegram", "Телеграм-каналы", ("telegram_channel", "telegram_community")),
)
# Вторичные источники без подтверждения не бывают важнее 6 и в главное не попадают.
# Чтобы выпуск рассказывал, что было на YouTube и в телеграме, у этих рубрик свой добор:
# до COMMUNITY_TOP самых интересных событий важностью от COMMUNITY_MIN.
COMMUNITY = ("discuss", "youtube", "telegram")
COMMUNITY_MIN = 4
COMMUNITY_TOP = 3


def section_of(ev: Event, sources: dict) -> str:
    src = sources.get(ev.lead["source"])
    kind = src.type if src else ""
    return next((key for key, _, types in SECTIONS if kind in types), "releases")


# Рассылки дают ссылки-кандидаты: агент разворачивает их в первоисточники
# (confirmed_by). Событие, о котором есть только рассылка без первоисточника, в
# выпуск не идёт — темой выпуска рассылка не бывает.
RADAR_ONLY = ("newsletter",)


def radar_only(ev: Event, sources: dict) -> bool:
    types = {getattr(sources.get(i["source"]), "type", "") for i in ev.items}
    expanded = any(i.get("confirmed_by") for i in ev.items)
    return types <= set(RADAR_ONLY) and not expanded


def estimate_words(main: list[Event], extras: int) -> int:
    """Длина пересказа до его написания: рамка, темы главного, добор."""
    themes = sum(BRIEF_WORDS if e.brief else FULL_WORDS for e in main)
    return FRAME_WORDS + themes + extras * EXTRA_WORDS


def select_sections(
    events: list[Event],
    sources: dict,
    minimum: int = DIGEST_MIN,
    recent: Collection[str] = (),
    top: int | None = None,
    budget: int = DIGEST_TARGET_WORDS,
) -> list[tuple[str, list[Event]]]:
    """Выпуск по рубрикам: все обязательные и важные события, добор — по бюджету.

    1. Главное (select): все важные с поправкой на поставщика (diversify, recent —
       поставщики главного прошлых дней), тихий день — добор 5–6.
    2. Не влезает в budget слов — менее значимые темы главного коротко (brief),
       обязательные — в последнюю очередь.
    3. Остался бюджет — лучший разбор от BLOG_MIN, RADAR_TOP материалов радара,
       до COMMUNITY_TOP из YouTube и телеграма: по убыванию важности, пока
       влезают. Настоящие новости они не вытесняют.

    Пустые рубрики не попадают; внутри рубрики — по убыванию важности.
    """
    events = [e for e in events if not radar_only(e, sources)]
    for e in events:
        e.brief = e.main = False
    ranked, level = diversify(events, sources, recent)
    main = select(ranked, minimum, level, top)
    for e in main:
        e.main = True
    # от менее значимого к более; самое важное событие всегда подробно
    place = {e.key: n for n, e in enumerate(main)}
    lesser = sorted(main, key=lambda e: (must_know(e), level(e), e.score, -place[e.key]))
    for ev in lesser[:-1]:
        if estimate_words(main, 0) <= budget:
            break
        ev.brief = True
    keys = {e.key for e in main}
    limits = {"blogs": 1, "discuss": RADAR_TOP, "youtube": COMMUNITY_TOP}
    limits["telegram"] = COMMUNITY_TOP
    if any(section_of(e, sources) == "blogs" for e in main):
        limits["blogs"] = 0
    floor = {"blogs": BLOG_MIN}
    extras: list[Event] = []
    for ev in events:  # по убыванию важности
        rubric = section_of(ev, sources)
        if ev.key in keys or rubric not in limits or limits[rubric] <= 0:
            continue
        if ev.importance < floor.get(rubric, COMMUNITY_MIN):
            continue
        if estimate_words(main, len(extras) + 1) > budget:
            break
        extras.append(ev)
        limits[rubric] -= 1
    keys |= {e.key for e in extras}
    order = ranked + [e for e in events if e not in ranked]  # главное — с поправкой
    out = []
    for key, title, _ in SECTIONS:
        part = [e for e in order if e.key in keys and section_of(e, sources) == key]
        if part:
            out.append((title, part))
    return out


def delivery(ev: Event) -> str:
    """Как подать событие в пересказе — строка «подача» в events.md."""
    if ev.brief:
        return "главное, но коротко — 2–3 предложения: выпуск длинный"
    if ev.main:
        return "главное, подробно — 2–3 абзаца"
    return "добор рубрики — 2–4 предложения"


def issue_length(sections: list[tuple[str, list[Event]]]) -> str:
    """Строка для events.md: оценка длины выпуска и что делать, если он велик."""
    events = [e for _, part in sections for e in part]
    main = [e for e in events if e.main]
    words = estimate_words(main, len(events) - len(main))
    low, high = DIGEST_TARGET_MINUTES
    text = (
        f"Оценка длины: ~{words} слов, ~{round(words / WORDS_PER_MIN_SPEAKER)} мин"
        f" (цель {low}–{high} мин, предел {DIGEST_MAX_MINUTES})."
    )
    if words > DIGEST_MAX_WORDS:
        text += (
            " Даже коротко выпуск длиннее предела: главное — подробно, остальное"
            " одной фразой на тему, а самую крупную тему вынеси в спецвыпуск."
        )
    return text


def select_weekly(events: list[Event], minimum: int = WEEKLY_MIN) -> list[Event]:
    """События обзора недели: все важные (от 7) без предела; мало — добрать 5–6.

    Длина видео следует за неделей: много важного — длинный обзор, мало — короткий.
    """
    strong = [e for e in events if e.importance >= IMPORTANT]
    if len(strong) >= minimum:
        return strong
    extra = [e for e in events if NOTABLE <= e.importance < IMPORTANT]
    return strong + extra[: minimum - len(strong)]


def factor(event: Event, name: str) -> float:
    """Наибольшее значение фактора среди материалов события."""
    return max((json.loads(i["factors"] or "{}").get(name, 0.0) for i in event.items), default=0.0)


def select_monthly(events: list[Event]) -> list[tuple[str, list[Event]]]:
    """Обзор месяца двумя рубриками: важное для разработчика и громкое в индустрии.

    Важное — как в обзоре недели. Громкое — события, о которых говорили все
    (community_signal или global_impact от BUZZ_SIGNAL), но оценка под Go и
    backend дала им меньше 7: сделки, инциденты, споры, модели.
    """
    strong = select_weekly(events)
    chosen = {e.key for e in strong}
    buzz = [
        e
        for e in events
        if e.key not in chosen
        and e.importance >= COMMUNITY_MIN
        and max(factor(e, "community_signal"), factor(e, "global_impact")) >= BUZZ_SIGNAL
    ]
    buzz.sort(key=lambda e: -(factor(e, "community_signal") + factor(e, "global_impact")))
    out = [(MONTHLY_SECTIONS[0], strong), (MONTHLY_SECTIONS[1], buzz[:MONTHLY_BUZZ])]
    return [(name, part) for name, part in out if part]


# --- текст для суфлёра: teleprompter.md ------------------------------------

_BRACKET = re.compile(r"\[[^\]]*\]")
_INSERT = re.compile(r"\[(M\d+)\]")  # [M1] — вставка при монтаже, описана в materials.md
_MARK = re.compile(r"/[^/\s]+/")  # /пауза/
_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
_MATERIAL = re.compile(r"^##\s+(M\d+)\b", re.M)  # ## M1 · видео


def spoken(text: str) -> str:
    """То, что ведущий читает вслух: без заголовков, вставок и пауз."""
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    body = _BRACKET.sub(" ", "\n".join(lines))
    return _MARK.sub(" ", body).replace("**", "")


def inserts(text: str) -> list[str]:
    return _INSERT.findall(text)


def check_teleprompter(text: str) -> list[str]:
    """Суфлёр — только то, что читают вслух, и короткие метки вставок [M1]."""
    problems = []
    if re.search(r"^\s*#", text, re.M):
        problems.append("заголовки не читают — план блоков и время веди в materials.md")
    other = [b for b in _BRACKET.findall(text) if not _INSERT.fullmatch(b)]
    if other:
        sample = ", ".join(other[:3])
        problems.append(
            f"в квадратных скобках только метки вставок [M1], [M2]… — описание вставки"
            f" перенеси в materials.md: {sample}"
        )
    if _MARK.search(_BRACKET.sub(" ", text)) or "**" in text:
        problems.append("без /пауз/ и **выделений** — паузу обозначь пустой строкой")
    names = inserts(text)
    expected = [f"M{n}" for n in range(1, len(names) + 1)]
    if names != expected:
        problems.append(
            "метки вставок нумеруй по порядку M1, M2… без пропусков и повторов, сейчас: "
            + ", ".join(names[:8])
        )
    said = spoken(text)
    if re.search(r"https?://|`", said):
        problems.append("ссылки и код вслух не читают — перенеси их в materials.md или post.md")
    long = [
        s.strip()
        for s in _SENTENCE.split(" ".join(said.split()))
        if len(s.split()) > TELEPROMPTER_MAX_WORDS
    ]
    if long:
        sample = "; ".join(f"«{' '.join(s.split()[:8])}…»" for s in long[:3])
        problems.append(
            f"{len(long)} фраз длиннее {TELEPROMPTER_MAX_WORDS} слов — с суфлёра их не"
            f" прочитать на одном дыхании, разбей: {sample}"
        )
    return problems


def check_materials(prompter: str, materials: str) -> list[str]:
    """Каждой метке в суфлёре — свой раздел `## M1 · …` в materials.md, и наоборот."""
    used, described = set(inserts(prompter)), _MATERIAL.findall(materials)
    problems = []
    if missing := sorted(used - set(described), key=lambda m: int(m[1:])):
        problems.append(f"нет описания вставок: {', '.join(missing)}")
    if extra := [m for m in described if m not in used]:
        problems.append(f"описаны, но в суфлёре их нет: {', '.join(extra)}")
    return problems


def minutes(text: str, words_per_min: int) -> float:
    return len(text.split()) / words_per_min


# Латинские буквы, как их читают вслух: «эс эс аш», «джи пи ти».
LETTER_NAMES = frozenset(
    "эй би си ди эф джи эйч ай джей кей эл эм эн оу пи кью ар эс ти ю ви дабл экс уай зед"
    " зи бэ вэ гэ дэ жэ зэ ка пэ тэ цэ эр аш икс эль".split()
)
_PUNCT = ".,:;!?«»()[]\"'—–-…*"
_ABBR = re.compile(r"[A-ZА-ЯЁ]{2,}")  # SSH, RSA, ML-KEM, OpenAI, ЦОД
_TECHY = re.compile(r"[A-Za-z].*\d|\d.*[A-Za-z]|\w[/_.]\w")  # SHA-1, Ed25519, x/crypto


def jargon(text: str) -> list[str]:
    """Что трудно слушать: аббревиатуры, версии с буквами, пути, имена букв подряд.

    Названия продуктов словом (GitHub, Postgres, Claude) не считаются.
    """
    found: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if len(run) >= 2:
            found.append(" ".join(run))
        run.clear()

    for raw in text.split():
        token = raw.strip(_PUNCT)
        if token.lower() in LETTER_NAMES:
            run.append(token.lower())
            continue
        flush()
        if _ABBR.search(token) or _TECHY.search(token):
            found.append(token)
    flush()
    return found


DIGEST_HEAD = (
    "Собрано `narrate news digest`. По этому списку пишутся narration.md, text.md и post.md."
)
WEEKLY_HEAD = "Собрано `narrate news weekly`. По этому списку пишутся teleprompter.md и post.md."
MONTHLY_HEAD = (
    "Собрано `narrate news monthly`. По этому списку пишутся teleprompter.md и post.md:"
    " сюжеты месяца, а не пересказ недельных обзоров."
)


def render_events(
    events: list[Event],
    sources: dict,
    title: str = "# События дайджеста",
    head=DIGEST_HEAD,
    sections: list[tuple[str, list[Event]]] | None = None,
) -> str:
    """events.md — материал для агента: что вошло в выпуск и откуда.

    sections — рубрики дайджеста: тогда события идут под заголовками рубрик.
    """
    lines = [title, "", head, ""]
    if sections:
        lines += [issue_length(sections), ""]
    rubric = {id(e): name for name, part in sections or () for e in part}
    current = None
    for n, ev in enumerate(events, start=1):
        lead = ev.lead
        if sections and rubric.get(id(ev)) != current:
            current = rubric.get(id(ev))
            lines += [f"# Рубрика: {current}", ""]
        lines += [
            f"## {n}. {lead['title_ru']}",
            "",
            f"- событие: `{ev.key}`, важность {ev.importance}/10, балл {ev.score:.2f},"
            f" тип {lead['kind']}",
            f"- почему важно: {lead['why']}",
            f"- источников: {ev.sources}",
        ]
        if sections:
            lines.append(f"- подача: {delivery(ev)}")
        dailies = sorted({i["digest"] for i in ev.items if i.get("digest")})
        if dailies:
            lines.append("- было в дайджестах дня: " + ", ".join(dailies))
        weeklies = sorted({i["weekly"] for i in ev.items if i.get("weekly")})
        if weeklies:
            lines.append("- было в обзорах недели: " + ", ".join(weeklies))
        lines.append("- материалы:")
        for item in sorted(ev.items, key=lambda i: -i["score"]):
            src = sources.get(item["source"])
            name = src.name if src else item["source"]
            confirmed = f", подтверждено: {item['confirmed_by']}" if item["confirmed_by"] else ""
            when = (item.get("published") or "")[:10] or "дата неизвестна"
            lines.append(f"  - {name}: [{item['title']}]({item['url']}), {when}{confirmed}")
            if item["summary"]:
                lines.append(f"    > {item['summary']}")
        factors = json.loads(lead["factors"] or "{}")
        lines += ["- факторы: " + ", ".join(f"{k} {v:g}" for k, v in factors.items()), ""]
    return "\n".join(lines)


# --- пост: post.md → HTML Telegram -----------------------------------------

_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_BOLD = re.compile(r"\*\*(.+?)\*\*")


def post_html(text: str) -> str:
    """Разметка поста: **жирный** и [текст](url) → HTML, остальное экранируется."""
    out = []
    for line in text.strip().splitlines():
        pieces, last = [], 0
        for m in _LINK.finditer(line):
            pieces.append(_bold(html.escape(line[last : m.start()], quote=False)))
            label = _bold(html.escape(m.group(1), quote=False))
            pieces.append(f'<a href="{html.escape(m.group(2))}">{label}</a>')
            last = m.end()
        pieces.append(_bold(html.escape(line[last:], quote=False)))
        out.append("".join(pieces).rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out))


def _bold(escaped: str) -> str:
    return _BOLD.sub(r"<b>\1</b>", escaped)


def visible_length(markup: str) -> int:
    """Сколько знаков увидит читатель: Telegram считает лимит без тегов."""
    return len(html.unescape(re.sub(r"<[^>]+>", "", markup)))


# --- судья: review.md --------------------------------------------------------

REVIEW = "review.md"  # отчёт судьи: вердикт и что проверено, что исправлено
VERDICTS = ("готово", "готово с оговорками", "не готово")
_VERDICT = re.compile(r"^\**Вердикт:?\**:?\s*(.+?)\s*\**\s*$", re.M | re.I)


def review_verdict(text: str) -> str | None:
    """Вердикт судьи из строки «Вердикт: …»; None — строки нет или вердикт не из VERDICTS."""
    m = _VERDICT.search(text)
    if not m:
        return None
    value = m[1].strip(" .*—-").lower()
    return value if value in VERDICTS else None


def check_post(text: str) -> list[str]:
    """Ошибки post.md: пусто, длинно, битая разметка."""
    problems = []
    if not text.strip():
        return ["post.md пустой"]
    body = post_html(text)
    size = visible_length(body)
    if size > POST_LIMIT:
        problems.append(f"post.md: {size} знаков, предел {POST_LIMIT} — сократи")
    if broken_markup(text):
        problems.append("post.md: незакрытая разметка — **жирный** или [текст](https://…)")
    return problems


def broken_markup(text: str) -> bool:
    """В тексте поста остались `**` без пары или ссылка не вида [текст](https://…)."""
    leftovers = _BOLD.sub("", _LINK.sub("", text))
    return "**" in leftovers or bool(re.search(r"\]\(", leftovers))


def headlines(text: str, limit: int = 6) -> list[str]:
    """Строки-темы поста без разметки и эмодзи — для обложки-карточки."""
    out = []
    for line in text.strip().splitlines()[1:]:
        plain = _LINK.sub(r"\1", line).replace("**", "")
        plain = re.sub(r"^[^\wА-Яа-яЁё]+", "", plain).strip()
        if plain:
            out.append(plain)
    return out[:limit]
