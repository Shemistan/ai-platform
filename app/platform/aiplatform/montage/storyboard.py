"""Раскадровка (MASTER_SPEC §5.4, §7): на каждый бит — нужна ли сцена, какой шаблон и props.

LLM (`### task: montage_storyboard`) — раскадровщик с оценкой нужности картинки (clarity, novelty,
cognitive_load, production_cost, factual_risk, 1–5). Сцена ставится, если score ≥ MIN_SCORE и не
теснее MIN_GAP_S к соседям; не «каждые N секунд». Сцена с factual_risk ≥ 4 без утверждения из
карты знаний отбрасывается: картинка не должна утверждать то, чего нет в источниках (§5.3).

Якорь сцены — слово исходника (`anchor_word`, индекс в words.json): он переживает правку плана,
а кадры результата пересчитываются по TimeMap перед каждой сборкой (`reanchor`). Хэш сцены —
по шаблону, props, длительности и ревизии стиля: перерисовывается только то, что изменилось (§6).
"""

from __future__ import annotations

import hashlib
import json

from aiplatform.montage import knowledge, narrative, render, schemas, templates
from aiplatform.montage import style as style_module

TASK = "montage_storyboard"
MIN_SCORE = 16  # из 25: «всё по 3» даёт 15 — сцена должна быть лучше среднего, а не «для оживления»
MIN_GAP_S = 20.0
HIGH_RISK = 4
NECESSITY = ("clarity", "novelty", "cognitive_load", "production_cost", "factual_risk")
STATUSES = ("proposed", "approved", "rendered", "blocked", "failed", "disabled")
LIMITS = {"intent": 200, "rationale": 300}
RULES = """Ты — раскадровщик и креативный директор технического YouTube-канала. Автор в кадре объясняет
тему; поверх него можно поставить короткие моушн-сцены по шаблонам. Для каждого бита реши, нужна ли
сцена, и если да — какой шаблон и с какими props.
Оцени нужность картинки по пяти шкалам 1–5: clarity (насколько картинка проясняет сказанное),
novelty (новое для зрителя или повтор слов), cognitive_load (нагрузка: 1 — легко, 5 — перегруз),
production_cost (1 — дёшево, 5 — дорого), factual_risk (1 — картинка ничего не утверждает,
5 — утверждает техническую деталь, которой нет в источниках).
Правила:
- Сцена только там, где она добавляет смысл: механизм, структура, список, цифра, сравнение, код,
  новая глава. Не ставь сцены «для оживления» и не чаще одной на 20 секунд.
- Техническая картинка (схема, код, планировщик) обязана опираться на утверждение карты знаний —
  укажи его id в claim_ids. Нет утверждения — не рисуй техническую деталь.
- Тексты в props — выжимка в 2–6 слов на элемент, как произносит автор; код — ровно то, о чём речь.
- intent — что сцена должна объяснить; rationale — почему именно этот шаблон.
- duration_s — 2.5–12 секунд; сцена начинается с первого слова бита (или со слова start_word)."""


def prompt(beats: list[dict], claims: list[dict], style: dict, title: str = "") -> str:
    return (
        f"### task: {TASK}\n{RULES}\n\n"
        f"Ролик: «{title or 'без названия'}».\n{style_module.describe(style)}\n\n"
        f"Шаблоны:\n{templates.hints()}\n\n"
        f"Утверждения карты знаний:\n{knowledge.pack_claims(claims)}\n\n"
        f"Биты (`id [первое–последнее слово] вид (приоритет, сложность): цель — «начало текста»`):\n"
        f"{narrative.pack_beats(beats)}\n\n"
        'Ответь только JSON: {"scenes": [{"beat_id": "beat_001", "template_id": "<id>", "props": {…}, '
        '"intent": "<что объяснить>", "claim_ids": ["claim_001"], "necessity": {"clarity": <1–5>, "novelty": <1–5>, '
        '"cognitive_load": <1–5>, "production_cost": <1–5>, "factual_risk": <1–5>}, "rationale": "<почему>", '
        '"start_word": <индекс слова или null>, "duration_s": <2.5–12>}]}\n'
    )


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _lvl(value: object, default: int = 3) -> int:
    try:
        return min(5, max(1, int(value)))
    except (TypeError, ValueError):
        return default


def score_of(necessity: dict) -> int:
    """0–25: польза (clarity, novelty) минус нагрузка, цена и риск."""
    return (necessity["clarity"] + necessity["novelty"] + (6 - necessity["cognitive_load"])
            + (6 - necessity["production_cost"]) + (6 - necessity["factual_risk"]))  # fmt: skip


def scene_hash(scene: dict) -> str:
    raw = json.dumps([templates.TEMPLATE_VERSION, scene["template_id"], scene["props"], scene["duration_frames"],
                      scene["fps"], scene["style_bible_revision"]], ensure_ascii=False, sort_keys=True)  # fmt: skip
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _animation(duration_frames: int, style: dict, fps: int) -> dict:
    enter = round(style["motion"]["enter_s"] * fps)
    exit_ = round(style["motion"]["exit_s"] * fps)
    return {"entrance_frames": enter, "hold_frames": max(0, duration_frames - enter - exit_), "exit_frames": exit_}


def clean(data: object, beats: list[dict], claims: list[dict], style: dict, out_words: list[dict],
          fps: int = render.FPS) -> list[dict]:  # fmt: skip
    """Сцены StoryboardScene: шаблон и props из каталога, порог score, интервал, риск без claim — прочь."""
    by_beat = {b["id"]: b for b in beats}
    by_src = {w["src"]: w for w in out_words}
    known_claims = {c["id"] for c in claims}
    items = data.get("scenes") if isinstance(data, dict) else None
    candidates: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        beat = by_beat.get(item.get("beat_id"))
        if beat is None or beat["end_frame"] <= beat["start_frame"]:
            continue
        try:
            tid = templates.resolve(item.get("template_id"))
            props = templates.clean_props(tid, item.get("props"))
        except templates.TemplateError:
            continue
        raw = item.get("necessity") if isinstance(item.get("necessity"), dict) else {}
        necessity = {k: _lvl(raw.get(k)) for k in NECESSITY}
        claim_ids = [c for c in item.get("claim_ids") or [] if isinstance(c, str) and c in known_claims][:6]
        if necessity["factual_risk"] >= HIGH_RISK and not claim_ids:
            continue
        score = score_of(necessity)
        if score < MIN_SCORE:
            continue
        anchor = beat["from"]
        start_word = item.get("start_word")
        if isinstance(start_word, int) and not isinstance(start_word, bool) and 0 <= start_word < len(out_words):
            src = out_words[start_word]["src"]
            if beat["from"] <= src <= beat["to"]:
                anchor = src
        kept = by_src.get(anchor) or next((by_src[i] for i in range(anchor, beat["to"] + 1) if i in by_src), None)
        if kept is None:
            continue
        try:
            duration_s = float(item.get("duration_s") or templates.get(tid).default_s)
        except (TypeError, ValueError):
            duration_s = templates.get(tid).default_s
        duration_s = min(templates.MAX_S, max(templates.MIN_S, duration_s))
        duration_frames = max(1, round(duration_s * fps))
        candidates.append({
            "schema_version": schemas.SCHEMA_VERSION, "id": "", "beat_id": beat["id"], "anchor_word": anchor,
            "edit_start_frame": kept["start_frame"], "duration_frames": duration_frames, "fps": {"num": fps, "den": 1},
            "intent": _text(item.get("intent"), LIMITS["intent"]) or beat["goal"], "grounding_claim_ids": claim_ids,
            "visual_type": templates.get(tid).visual_type, "engine": "hyperframes", "template_id": tid,
            "style_bible_revision": style["revision"], "props": props, "assets": [],
            "animation": _animation(duration_frames, style, fps),
            "layers": ["presenter", templates.get(tid).visual_type, "captions"], "status": "proposed", "enabled": True,
            "rationale": _text(item.get("rationale"), LIMITS["rationale"]), "necessity": necessity, "score": score,
            "hash": "", "error": None,
        })  # fmt: skip
    # Лучшие — первыми; не теснее MIN_GAP_S и не больше одной на бит.
    picked: list[dict] = []
    gap = MIN_GAP_S * fps
    for scene in sorted(candidates, key=lambda s: (-s["score"], s["edit_start_frame"])):
        if any(s["beat_id"] == scene["beat_id"] for s in picked):
            continue
        if all(abs(s["edit_start_frame"] - scene["edit_start_frame"]) >= gap for s in picked):
            picked.append(scene)
    picked.sort(key=lambda s: s["edit_start_frame"])
    for n, scene in enumerate(picked, 1):
        scene["id"] = f"scene_{n:03d}"
        scene["hash"] = scene_hash(scene)
    schemas.check("storyboard_scene", picked, many=True)
    return picked


def reanchor(scenes: list[dict], out_words: list[dict], beats: list[dict]) -> list[dict]:
    """Кадры сцен по текущей карте времени; якорь вырезан — следующее оставшееся слово бита, бита нет — blocked."""
    by_src = {w["src"]: w for w in out_words}
    by_beat = {b["id"]: b for b in beats}
    out = []
    for scene in scenes:
        item = dict(scene)
        beat = by_beat.get(scene["beat_id"])
        kept = by_src.get(scene["anchor_word"])
        if kept is None and beat:
            kept = next((by_src[i] for i in range(scene["anchor_word"], beat["to"] + 1) if i in by_src), None)
        if kept is None:
            item.update(status="blocked", error="слова сцены вырезаны из ролика", enabled=False)
        else:
            item["edit_start_frame"] = kept["start_frame"]
            if item["status"] == "blocked" and item.get("error", "").startswith("слова сцены"):
                item.update(status="proposed", error=None)
        out.append(item)
    return out


def with_style(scenes: list[dict], style: dict) -> list[dict]:
    """Новая ревизия стиля: сцены получают её и новый хэш — рендер заново (§6)."""
    out = []
    for scene in scenes:
        item = {**scene, "style_bible_revision": style["revision"]}
        item["animation"] = _animation(item["duration_frames"], style, item["fps"]["num"])
        new = scene_hash(item)
        if new != item["hash"]:
            item["hash"] = new
            if item["status"] == "rendered":
                item["status"] = "proposed"
        out.append(item)
    return out


class EditError(ValueError):
    pass


def edit(scene: dict, patch: object, max_frames: int) -> dict:
    """Правка человека: enabled, edit_start_frame, duration_frames, template_id, props. Спека изменилась —
    новый хэш, статус proposed (рендер заново только у неё)."""
    data = patch if isinstance(patch, dict) else {}
    item = json.loads(json.dumps(scene))
    if "enabled" in data:
        if not isinstance(data["enabled"], bool):
            raise EditError("enabled — true или false.")
        item["enabled"] = data["enabled"]
        if item["status"] == "disabled" and data["enabled"]:
            item["status"] = "proposed"
        elif not data["enabled"]:
            item["status"] = "disabled"
    for key in ("edit_start_frame", "duration_frames"):
        if key in data:
            value = data[key]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise EditError(f"{key} — целое число кадров.")
            item[key] = value
    if item["duration_frames"] < 1 or item["edit_start_frame"] + item["duration_frames"] > max_frames:
        raise EditError("Сцена выходит за конец ролика.")
    if "template_id" in data or "props" in data:
        try:
            tid = templates.resolve(data.get("template_id", item["template_id"]))
            base = item["props"] if tid == item["template_id"] else {}
            item["props"] = templates.clean_props(tid, data.get("props", base))
        except templates.TemplateError as exc:
            raise EditError(str(exc)) from exc
        item["template_id"] = tid
        item["visual_type"] = templates.get(tid).visual_type
        item["layers"] = ["presenter", item["visual_type"], "captions"]
    new = scene_hash(item)
    if new != item["hash"]:
        item["hash"] = new
        if item["status"] in ("rendered", "failed", "blocked"):
            item["status"] = "proposed"
            item["error"] = None
    schemas.check("storyboard_scene", item)
    return item


def summary(scenes: list[dict]) -> dict:
    by_status: dict[str, int] = {}
    by_template: dict[str, int] = {}
    for s in scenes:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        if s.get("enabled", True):
            by_template[s["template_id"]] = by_template.get(s["template_id"], 0) + 1
    return {"scenes": len(scenes), "enabled": sum(1 for s in scenes if s.get("enabled", True)),
            "by_status": by_status, "by_template": by_template}  # fmt: skip
