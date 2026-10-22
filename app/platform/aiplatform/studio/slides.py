"""План слайдов: разбор ответа LLM, абзацы и фразы текста и когда какой слайд на экране.

Слайд привязан к фразе: `para` — абзац, `sent` — номер фразы в абзаце (нет —
первая). Числа слайдов не ограничено. Вид слайда (`kind`): `image` —
сгенерированная картинка с заголовком и тезисами, остальные — динамические
сцены, которые рисует рендер (DYNAMIC). Данные сцен лежат в `data`.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field

from aiplatform.studio.llm import StudioError

MAX_TITLE = 90
MAX_BULLETS = 3  # тезисов поверх картинки
MAX_ITEMS = 5  # пунктов, шагов и строк сравнения у динамических слайдов
MAX_BULLET = 110
MAX_POINTS = 8  # точек графика
MAX_QUOTE = 280
MIN_SLIDE_S = 1.5
WORDS_PER_S = 2.2  # ~130 слов в минуту, как у оценки длины озвучки
SLIDE_SECONDS = (4, 6, 8, 10, 12)  # настройка slide_s: ориентир для LLM, сколько держится слайд
TARGET_SLIDE_S = 6
# Время на чтение (настройка reading): слов в секунду, которые зритель читает с экрана
# под озвучку. Слайд не сменится раньше, чем прочитается его текст.
READING = {"off": 0.0, "normal": 3.0, "slow": 2.0}
READ_BASE_S = 1.0  # найти текст глазами
READ_LAG_S = 1.0  # дольше время на чтение слайд не задерживает: речь уже о следующем (BE-016)
MAX_READ_S = 15.0  # код и длинные списки — смотрят, а не читают целиком

IMAGE = "image"
DYNAMIC = ("bullets", "stat", "chart", "flow", "compare", "quote", "chapter", "code", "summary")
CHAPTER = "chapter"
MAX_CODE_LINES = 14
MAX_CODE_LINE = 64
KINDS = (IMAGE, *DYNAMIC)
CHARTS = ("bar", "line")

_SENTENCE = re.compile(r"(?<=[.!?…])\s+")
_MARK = re.compile(r"\[\s*экран\s*(\d+)\s*\]", re.I)  # prompts.SCREEN_MARK


@dataclass
class Slide:
    para: int
    title: str
    bullets: list[str] = field(default_factory=list)
    image_prompt: str = ""
    video_prompt: str = ""
    sent: int = 0
    kind: str = IMAGE
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Как в plan.json: данные сцены — полями слайда, пустые поля опущены."""
        out = {k: v for k, v in asdict(self).items() if k != "data"}
        if not out["sent"]:
            del out["sent"]
        return out | self.data


def paragraphs(text: str) -> list[str]:
    """Абзацы — куски между пустыми строками, как их делит озвучка narrator; заголовки `# ` — тоже абзацы."""
    out, buf = [], []
    for line in text.splitlines() + [""]:
        if line.strip():
            buf.append(line.strip().lstrip("#").strip())
        elif buf:
            out.append(" ".join(buf))
            buf = []
    return [p for p in out if p]


def sentences(paragraph: str) -> list[str]:
    """Фразы абзаца: по точке, вопросу, восклицанию или многоточию и пробелу за ними."""
    return [s for s in _SENTENCE.split(paragraph.strip()) if s] or [paragraph]


def target_slides(paras: list[str], seconds: float = TARGET_SLIDE_S) -> int:
    """Сколько слайдов просить у LLM, чтобы слайд держался ~seconds секунд озвучки."""
    words = sum(len(p.split()) for p in paras)
    return max(1, round(words / WORDS_PER_S / (seconds or TARGET_SLIDE_S)))


def slide_words(slide: Slide) -> int:
    """Сколько слов на экране: заголовок, тезисы и тексты сцены (пункты, шаги, подписи, цитата)."""

    def count(value: object) -> int:
        if isinstance(value, str):
            return len(value.split())
        if isinstance(value, dict):
            return sum(count(v) for k, v in value.items() if k not in ("chart", "number"))
        if isinstance(value, list):
            return sum(count(v) for v in value)
        return 1 if isinstance(value, int | float) and not isinstance(value, bool) else 0

    return count(slide.title) + count(slide.bullets) + count(slide.data)


def reading_s(slide: Slide, words_per_s: float) -> float:
    """Сколько секунд нужно, чтобы прочитать слайд; 0 — время на чтение выключено."""
    if words_per_s <= 0:
        return 0.0
    return min(READ_BASE_S + slide_words(slide) / words_per_s, MAX_READ_S)


def _short(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value) if math.isfinite(value) else None
    text = str(value or "").replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def _texts(value: object, limit: int, count: int) -> list[str]:
    items = value if isinstance(value, list) else []
    return [_short(v, limit) for v in items if str(v or "").strip()][:count]


def _side(value: object) -> dict:
    side = value if isinstance(value, dict) else {}
    return {"title": _short(side.get("title"), 40), "items": _texts(side.get("items"), MAX_BULLET, MAX_ITEMS)}


def scene_data(kind: str, item: dict) -> dict | None:
    """Данные динамической сцены из слайда плана. None — данных не хватает, слайд станет пунктами."""
    if kind == "stat":
        value = _number(item.get("value"))
        if value is None:
            return None
        return {
            "value": value,
            "prefix": _short(item.get("prefix"), 6),
            "suffix": _short(item.get("suffix"), 12),
            "label": _short(item.get("label"), MAX_BULLET),
        }
    if kind == "chart":
        points = []
        for point in item.get("data") if isinstance(item.get("data"), list) else []:
            value = _number(point.get("value")) if isinstance(point, dict) else None
            if value is not None:
                points.append({"label": _short(point.get("label"), 16), "value": value})
        if len(points) < 2:
            return None
        chart = item.get("chart") if item.get("chart") in CHARTS else CHARTS[0]
        return {"chart": chart, "unit": _short(item.get("unit"), 10), "data": points[:MAX_POINTS]}
    if kind == "flow":
        steps = _texts(item.get("steps"), 40, MAX_ITEMS)
        return {"steps": steps} if len(steps) >= 2 else None
    if kind == "compare":
        left, right = _side(item.get("left")), _side(item.get("right"))
        return {"left": left, "right": right} if left["items"] and right["items"] else None
    if kind == "quote":
        text = _short(item.get("text"), MAX_QUOTE)
        return {"text": text, "author": _short(item.get("author"), 60)} if text else None
    if kind == CHAPTER:
        return {"subtitle": _short(item.get("subtitle"), MAX_BULLET)}
    if kind == "code":
        raw = str(item.get("code") or "").replace("\t", "    ").rstrip()
        lines = [line.rstrip()[:MAX_CODE_LINE] for line in raw.splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        lines = lines[:MAX_CODE_LINES]
        if not any(line.strip() for line in lines):
            return None
        return {"code": "\n".join(lines), "language": _short(item.get("language"), 16)}
    if kind == "summary":
        items = _texts(item.get("items") or item.get("bullets"), MAX_BULLET, MAX_ITEMS)
        return {"items": items} if len(items) >= 2 else None
    return {}


def _slide(item: dict, para: int, sent: int, title: str) -> Slide:
    """Слайд с видом: без вида — картинка, если есть её промпт, иначе пункты."""
    image_prompt = _short(item.get("image_prompt"), 600)
    kind = item.get("kind") if item.get("kind") in KINDS else (IMAGE if image_prompt else "bullets")
    if kind == IMAGE and not image_prompt:
        kind = "bullets"
    data = scene_data(kind, item)
    if data is None:
        kind, data = "bullets", {}
    count = MAX_BULLETS if kind == IMAGE else MAX_ITEMS
    slide = Slide(
        para=para,
        title=title,
        bullets=_texts(item.get("bullets"), MAX_BULLET, count),
        sent=sent,
        kind=kind,
        data=data,
    )
    if kind == IMAGE:  # картинки и ролики — только у слайдов-картинок
        slide.image_prompt = image_prompt
        slide.video_prompt = _short(item.get("video_prompt"), 600)
    return slide


def parse_plan(obj: object, paras: list[str] | int) -> list[Slide]:
    """Слайды из JSON модели (или правки человека): абзацы и фразы зажаты в текст,
    по возрастанию, без повторов, первый — с начала текста. Ни одного — ошибка.

    paras — абзацы текста (по ним зажимаются номера фраз) или только их число."""
    raw = obj.get("slides") if isinstance(obj, dict) else obj
    if not isinstance(raw, list):
        raise StudioError("В плане нет списка slides.")
    count = paras if isinstance(paras, int) else len(paras)
    slides: dict[tuple[int, int], Slide] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        para = min(max(_int(item.get("para", 0)), 0), max(count - 1, 0))
        sent = max(_int(item.get("sent", 0)), 0)
        if not isinstance(paras, int) and paras:
            sent = min(sent, len(sentences(paras[para])) - 1)
        title = _short(item.get("title"), MAX_TITLE)
        if not title or (para, sent) in slides:
            continue
        slides[(para, sent)] = _slide(item, para, sent, title)
    if not slides:
        raise StudioError("В плане ни одного слайда с заголовком.")
    ordered = [slides[k] for k in sorted(slides)]
    ordered[0].para, ordered[0].sent = 0, 0
    number = 0
    for slide in ordered:  # главы нумеруются по порядку: «Глава 2» — вторая из встретившихся
        if slide.kind == CHAPTER:
            number += 1
            slide.data["number"] = number
    return ordered


def chapters(plan: list[Slide]) -> list[str]:
    """Название главы, в которой идёт каждый слайд; до первой главы — пусто."""
    out, current = [], ""
    for slide in plan:
        if slide.kind == CHAPTER:
            current = slide.title
        out.append(current)
    return out


def split_screen(marked: str, items: list[dict]) -> tuple[str, list[dict]]:
    """Текст озвучки с метками «[экран N]» → текст без меток и код для экрана с местом показа.

    Место (para, sent) — по абзацам и фразам чистого текста, как их делит `paragraphs`:
    фраза, перед которой стоит метка. Абзац из одной метки — код к началу следующего.
    Код без метки показывается с начала последнего абзаца; метка без кода — просто снимается."""
    out, inserts, pending = [], [], []
    count = 0
    for block in re.split(r"\n[ \t]*\n", marked.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        joined = " ".join(line.strip().lstrip("#").strip() for line in lines)
        found = []
        for m in _MARK.finditer(joined):
            prefix = _MARK.sub(" ", joined[: m.start()]).split()
            sent = len(sentences(" ".join(prefix) + " x")) - 1 if prefix else 0
            found.append((int(m[1]), sent))
        clean = [" ".join(_MARK.sub(" ", line).split()) for line in lines]
        clean = [line for line in clean if line.lstrip("#").strip()]
        if not clean:
            pending += [n for n, _ in found]
            continue
        out.append("\n".join(clean))
        inserts += [(n, count, 0) for n in pending] + [(n, count, s) for n, s in found]
        pending = []
        count += 1
    placed = {}
    for n, para, sent in inserts:
        placed.setdefault(n, (para, sent))
    screen = []
    for n, item in enumerate(items, start=1):
        data = scene_data("code", item) if isinstance(item, dict) else None
        if data is None:
            continue
        para, sent = placed.get(n, (max(count - 1, 0), 0))
        title = _short(item.get("title"), MAX_TITLE) or "Код"
        screen.append({"para": para, "sent": sent, "kind": "code", "title": title} | data)
    return "\n\n".join(out), screen


def with_screen(plan: list[Slide], screen: list[dict], paras: list[str]) -> list[Slide]:
    """План с кодом для экрана: слайд кода встаёт на свою фразу, вытесняя слайд модели на ней."""
    taken = {(s.para, s.sent): s for s in plan}
    for item in screen:
        para = min(max(_int(item.get("para")), 0), max(len(paras) - 1, 0))
        sent = min(max(_int(item.get("sent")), 0), len(sentences(paras[para])) - 1) if paras else 0
        title = _short(item.get("title"), MAX_TITLE) or "Код"
        taken[(para, sent)] = _slide(item | {"kind": "code"}, para, sent, title)
    return [taken[k] for k in sorted(taken)]


def fallback_plan(paras: list[str], title: str) -> list[Slide]:
    """Один слайд на весь текст — если модель недоступна, а видео нужно."""
    return [Slide(para=0, title=_short(title or (paras[0] if paras else "Видео"), MAX_TITLE))]


def words_before(paras: list[str], para: int, sent: int) -> int:
    """Сколько слов звучит до фразы sent абзаца para."""
    before = sum(len(p.split()) for p in paras[:para])
    if para < len(paras) and sent:
        before += sum(len(s.split()) for s in sentences(paras[para])[:sent])
    return before


def starts(paras: list[str], slides: list[Slide], total_s: float) -> list[float]:
    """Секунда, с которой звучит фраза слайда: доля слов до неё × длина аудио."""
    all_words = sum(len(p.split()) for p in paras) or 1
    return [total_s * words_before(paras, s.para, s.sent) / all_words for s in slides]


def spoken(paras: list[str], plan: list[Slide], n: int) -> str:
    """Текст, который звучит, пока на экране слайд n: от его фразы до фразы следующего слайда."""
    flat = [(p, s, text) for p, para in enumerate(paras) for s, text in enumerate(sentences(para))]
    begin = (plan[n].para, plan[n].sent)
    end = (plan[n + 1].para, plan[n + 1].sent) if n + 1 < len(plan) else (len(paras), 0)
    said = [text for p, s, text in flat if begin <= (p, s) < end]
    return " ".join(said or [text for p, s, text in flat if (p, s) >= begin][:1])


def _begins(
    paras: list[str], slides: list[Slide], total_s: float, intro_s: float, spoken_at: list[float] | None = None
) -> list[float]:
    """С какой секунды после интро звучит фраза каждого слайда.

    spoken_at — когда фраза звучит на деле, по паузам в звуке (studio/timing.py); нет — по доле слов."""
    at = spoken_at if spoken_at is not None else starts(paras, slides, total_s)
    begin = [max(s - intro_s, 0.0) for s in at]
    begin[0] = 0.0
    return begin


def durations(
    paras: list[str],
    slides: list[Slide],
    total_s: float,
    intro_s: float = 0.0,
    minimums: list[float] | None = None,
    spoken_at: list[float] | None = None,
) -> list[float]:
    """Сколько секунд держится каждый слайд, чтобы вместе с интро ровно покрыть аудио.

    Первые intro_s секунд занимает постер. Слайд встаёт на свою фразу (spoken_at —
    когда она звучит на деле) и не короче MIN_SLIDE_S. minimums (время на чтение)
    задерживает следующий слайд не дольше READ_LAG_S после его фразы: иначе речь
    уходит вперёд картинки (BE-016). Не хватает аудио на все минимумы — все
    сжимаются в одной доле.
    """
    if not slides:
        return []
    body = max(total_s - intro_s, 0.0)
    n = len(slides)
    reads = minimums or [0.0] * n
    floors = [max(MIN_SLIDE_S, m) for m in reads]
    if sum(floors) > body:
        share = body / sum(floors)
        floors, reads = [f * share for f in floors], [r * share for r in reads]
    spoken = _begins(paras, slides, total_s, intro_s, spoken_at)
    begin = list(spoken)
    for i in range(1, n):
        read = min(begin[i - 1] + reads[i - 1], spoken[i] + READ_LAG_S)
        begin[i] = max(begin[i], begin[i - 1] + min(MIN_SLIDE_S, floors[i - 1]), read)
    # Не вылезти за конец: последние слайды поджимаются назад, оставляя каждому его floor.
    for i in range(n - 1, -1, -1):
        limit = body - sum(floors[i:])
        begin[i] = min(begin[i], max(limit, 0.0))
    ends = begin[1:] + [body]
    return [round(max(e - b, 0.0), 3) for b, e in zip(begin, ends, strict=True)]


def lag(
    paras: list[str],
    slides: list[Slide],
    seconds: list[float],
    total_s: float,
    intro_s: float = 0.0,
    spoken_at: list[float] | None = None,
) -> float:
    """На сколько секунд слайд самое большее отстаёт от своей фразы в озвучке."""
    if not slides:
        return 0.0
    shown = [sum(seconds[:i]) for i in range(len(slides))]
    begins = _begins(paras, slides, total_s, intro_s, spoken_at)
    return round(max(s - b for s, b in zip(shown, begins, strict=True)), 1)
