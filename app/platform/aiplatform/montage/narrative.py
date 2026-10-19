"""Нарративные биты (MASTER_SPEC §5.3): отрезки речи с целью, сложностью и нужной картинкой.

LLM (`### task: montage_beats`) видит расшифровку результата и утверждения карты знаний, делит речь
на биты: hook, setup, explanation, demo, recap, cta. Индексы слов результата переводятся в индексы
исходника (`src`) — якорь, который переживает правку плана; кадры результата считаются по TimeMap
заново при каждой сборке (`with_frames`).
"""

from __future__ import annotations

from aiplatform.montage import knowledge, plan

TASK = "montage_beats"
PURPOSES = ("hook", "setup", "explanation", "demo", "recap", "cta", "other")
MAX_BEATS = 80
LIMITS = {"goal": 200, "visuals_needed": 200, "keyword": 40}
RULES = """Ты — редактор нарратива YouTube-канала. Раздели расшифровку смонтированного ролика на биты —
связные отрезки речи, у каждого своя цель. Виды: hook (зачем смотреть), setup (контекст, термины),
explanation (как устроено), demo (показ на экране), recap (итог), cta (призыв), other.
Для каждого бита: цель одной фразой, сложность для зрителя 1–5, ключевые слова, какая картинка
помогла бы (или «нет»), какие утверждения карты знаний в нём звучат (их id), приоритет 1–5
(5 — сердце ролика), уверенность 0–1.
Правила: биты идут подряд и не пересекаются; границы — на границах фраз; бит — от 15 секунд до
3 минут; не придумывай утверждений, которых нет в расшифровке; противоречивые утверждения
(contradicted) отметь в цели бита словом «спорно»."""


def prompt(packed_words: str, claims: list[dict], title: str = "") -> str:
    return (
        f"### task: {TASK}\n{RULES}\n\n"
        f"Ролик: «{title or 'без названия'}».\nУтверждения карты знаний:\n{knowledge.pack_claims(claims)}\n\n"
        'Ответь только JSON: {"beats": [{"from": <индекс первого слова>, "to": <индекс последнего>, '
        f'"purpose": "<{"|".join(PURPOSES)}>", "goal": "<цель>", "difficulty": <1–5>, "keywords": ["…"], '
        '"visuals_needed": "<что показать или нет>", "claims": ["claim_001"], "priority": <1–5>, '
        '"confidence": <0–1>}]}\n\n'
        f"Расшифровка:\n{packed_words}\n"
    )


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _int(value: object, lo: int, hi: int, default: int) -> int:
    try:
        return min(hi, max(lo, int(value)))
    except (TypeError, ValueError):
        return default


def clean(data: object, out_words: list[dict], claims: list[dict]) -> list[dict]:
    """Биты по порядку, без пересечений, индексы → исходник; утверждения — только известные."""
    known = {c["id"] for c in claims}
    items = data.get("beats") if isinstance(data, dict) else None
    raw: list[tuple[int, int, dict]] = []
    n = len(out_words)
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not n:
            continue
        try:
            a, b = int(item.get("from")), int(item.get("to"))
        except (TypeError, ValueError):
            continue
        a, b = max(0, min(a, b)), min(n - 1, max(a, b))
        if a <= b:
            raw.append((a, b, item))
    raw.sort(key=lambda x: (x[0], x[1]))
    out: list[dict] = []
    last = -1
    for a, b, item in raw:
        a = max(a, last + 1)
        if a > b:
            continue
        last = b
        purpose = item.get("purpose") if item.get("purpose") in PURPOSES else "other"
        keywords = [_text(k, LIMITS["keyword"]) for k in item.get("keywords") or [] if isinstance(k, str)][:8]
        claim_ids = [c for c in item.get("claims") or [] if isinstance(c, str) and c in known][:10]
        try:
            confidence = min(1.0, max(0.0, float(item.get("confidence", 0.6))))
        except (TypeError, ValueError):
            confidence = 0.6
        out.append({
            "id": f"beat_{len(out) + 1:03d}", "from": out_words[a]["src"], "to": out_words[b]["src"],
            "start_frame": out_words[a]["start_frame"], "end_frame": out_words[b]["end_frame"],
            "purpose": purpose, "goal": _text(item.get("goal"), LIMITS["goal"]) or purpose,
            "difficulty": _int(item.get("difficulty"), 1, 5, 3), "keywords": [k for k in keywords if k],
            "visuals_needed": _text(item.get("visuals_needed"), LIMITS["visuals_needed"]),
            "claims": claim_ids, "priority": _int(item.get("priority"), 1, 5, 3), "confidence": round(confidence, 2),
            "text": " ".join(w["w"] for w in out_words[a : min(b + 1, a + 30)]),
        })  # fmt: skip
        if len(out) >= MAX_BEATS:
            break
    return out


def with_frames(beats: list[dict], out_words: list[dict]) -> list[dict]:
    """Кадры битов по текущей карте времени: первое и последнее оставшееся слово исходника в диапазоне.
    Бит, вырезанный целиком, получает start_frame == end_frame и не ставит сцен."""
    by_src = {w["src"]: w for w in out_words}
    out = []
    for beat in beats:
        kept = [by_src[i] for i in range(beat["from"], beat["to"] + 1) if i in by_src]
        item = dict(beat)
        if kept:
            item["start_frame"], item["end_frame"] = kept[0]["start_frame"], kept[-1]["end_frame"]
        else:
            item["start_frame"] = item["end_frame"] = 0
        out.append(item)
    return out


def pacing(beats: list[dict], fps: int) -> dict:
    """Темп (§5.3): длительности, самая длинная череда «explanation», доля каждого вида."""
    if not beats:
        return {"beats": 0, "longest_explanation_s": 0.0, "share": {}}
    total = sum(max(0, b["end_frame"] - b["start_frame"]) for b in beats) or 1
    share: dict[str, float] = {}
    for b in beats:
        share[b["purpose"]] = share.get(b["purpose"], 0.0) + (b["end_frame"] - b["start_frame"]) / total
    longest = run = 0
    for b in beats:
        run = run + (b["end_frame"] - b["start_frame"]) if b["purpose"] == "explanation" else 0
        longest = max(longest, run)
    return {
        "beats": len(beats),
        "mean_s": round(total / len(beats) / fps, 1),
        "longest_explanation_s": round(longest / fps, 1),
        "share": {k: round(v, 2) for k, v in share.items()},
    }


def pack_beats(beats: list[dict]) -> str:
    """Биты для раскадровщика: `- beat_001 [from–to] explanation (p4, d3): цель — текст…`."""
    lines = []
    for b in beats:
        claims = (" " + " ".join(b["claims"])) if b["claims"] else ""
        lines.append(
            f"- {b['id']} [{b['from']}–{b['to']}] {b['purpose']} (p{b['priority']}, d{b['difficulty']}): "
            f"{b['goal']} — «{b.get('text', '')}»{claims}"
        )
    return "\n".join(lines) or "- (битов нет)"


pack = plan.pack
