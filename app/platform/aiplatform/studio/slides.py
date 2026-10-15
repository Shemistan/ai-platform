"""План слайдов: разбор ответа LLM, абзацы текста и когда какой слайд на экране."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from aiplatform.studio.llm import StudioError

MAX_SLIDES = 20
MAX_TITLE = 90
MAX_BULLETS = 3
MAX_BULLET = 110
MIN_SLIDE_S = 1.5


@dataclass
class Slide:
    para: int
    title: str
    bullets: list[str] = field(default_factory=list)
    image_prompt: str = ""
    video_prompt: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


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


def max_slides(count: int) -> int:
    return max(1, min(MAX_SLIDES, count))


def _short(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def parse_plan(obj: object, count: int) -> list[Slide]:
    """Слайды из JSON модели (или правки человека): номера абзацев зажаты в [0, count),
    по возрастанию, без повторов, первый — с абзаца 0. Ни одного — ошибка."""
    raw = obj.get("slides") if isinstance(obj, dict) else obj
    if not isinstance(raw, list):
        raise StudioError("В плане нет списка slides.")
    slides: dict[int, Slide] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            para = int(item.get("para", 0))
        except (TypeError, ValueError):
            para = 0
        para = min(max(para, 0), max(count - 1, 0))
        title = _short(item.get("title"), MAX_TITLE)
        if not title or para in slides:
            continue
        bullets = item.get("bullets") if isinstance(item.get("bullets"), list) else []
        slides[para] = Slide(
            para=para,
            title=title,
            bullets=[_short(b, MAX_BULLET) for b in bullets if str(b or "").strip()][:MAX_BULLETS],
            image_prompt=_short(item.get("image_prompt"), 600),
            video_prompt=_short(item.get("video_prompt"), 600),
        )
    if not slides:
        raise StudioError("В плане ни одного слайда с заголовком.")
    ordered = [slides[k] for k in sorted(slides)][:MAX_SLIDES]
    ordered[0].para = 0
    return ordered


def fallback_plan(paras: list[str], title: str) -> list[Slide]:
    """Один слайд на весь текст — если модель недоступна, а видео нужно."""
    return [Slide(para=0, title=_short(title or (paras[0] if paras else "Видео"), MAX_TITLE))]


def starts(paras: list[str], slides: list[Slide], total_s: float) -> list[float]:
    """Секунда, с которой звучит абзац слайда: доля слов до абзаца × длина аудио."""
    words = [len(p.split()) for p in paras]
    all_words = sum(words) or 1
    out = []
    for slide in slides:
        before = sum(words[: slide.para])
        out.append(total_s * before / all_words)
    return out


def durations(paras: list[str], slides: list[Slide], total_s: float, intro_s: float = 0.0) -> list[float]:
    """Сколько секунд держится каждый слайд, чтобы вместе с интро ровно покрыть аудио.

    Первые intro_s секунд занимает постер. Слайд не короче MIN_SLIDE_S (если
    аудио на всех хватает) — короткий сдвигает начало следующих.
    """
    if not slides:
        return []
    body = max(total_s - intro_s, 0.0)
    n = len(slides)
    floor = min(MIN_SLIDE_S, body / n) if n else 0.0
    begin = [max(s - intro_s, 0.0) for s in starts(paras, slides, total_s)]
    begin[0] = 0.0
    for i in range(1, n):
        begin[i] = max(begin[i], begin[i - 1] + floor)
    # Не вылезти за конец: последние слайды поджимаются назад, оставляя каждому floor.
    for i in range(n - 1, -1, -1):
        limit = body - floor * (n - i)
        begin[i] = min(begin[i], max(limit, 0.0))
    ends = begin[1:] + [body]
    return [round(max(e - b, 0.0), 3) for b, e in zip(begin, ends, strict=True)]
