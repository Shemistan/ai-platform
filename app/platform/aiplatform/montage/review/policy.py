"""Политика запуска ревью (ITERATIVE_REVIEW_SPEC §2, §6.6): настройки, профили, что безопасно и что защищено.

`clean` — проверка настроек от человека (ошибка → PolicyError с текстом для UI). `PROFILES` — веса
субъективных измерений судьи. `is_safe` — действие, которое auto_safe принимает без человека.
`protected` — действие трогает защищённые слова, сцены или утверждения. `budget_left` — что осталось
от лимитов. «GPU-минуты» здесь — минуты тяжёлых процессов (ffmpeg, HyperFrames, LLM): GPU-метрик у
машины нет (ADR 0012), считаем честно то, что измеримо.
"""

from __future__ import annotations

MODES = {"manual_approval": "Утверждаю каждый план", "auto_safe": "Безопасные правки сами",
         "autonomous": "Автономно в пределах политики"}  # fmt: skip
PROFILES = {
    "balanced": {"clarity": 0.2, "style": 0.15, "motion": 0.15, "pacing": 0.2, "distraction": 0.1, "accuracy": 0.2},
    "educational": {"clarity": 0.3, "style": 0.05, "motion": 0.1, "pacing": 0.15, "distraction": 0.1, "accuracy": 0.3},
    "dynamic_youtube": {"clarity": 0.15, "style": 0.2, "motion": 0.25, "pacing": 0.25, "distraction": 0.05,
                        "accuracy": 0.1},
    "minimalist": {"clarity": 0.25, "style": 0.1, "motion": 0.05, "pacing": 0.2, "distraction": 0.3, "accuracy": 0.1},
}  # fmt: skip
DIMENSIONS = tuple(PROFILES["balanced"])
ROLES = ("executive", "narrative", "retention", "motion_director", "art_director", "composition", "sound",
         "educational", "accessibility", "technical_qa")  # fmt: skip
DEFAULT = {
    "enabled": True,
    "max_rounds": 2,
    "mode": "manual_approval",
    "review_profile": "balanced",
    "quality_target": 0.8,
    "max_gpu_minutes": 30.0,
    "max_wall_time_minutes": 90.0,
    "max_new_assets": 12,
    "max_render_attempts_per_issue": 2,
    "allow_narrative_edits": False,
    "protected_ranges": [],
    "locked_scenes": [],
    "protected_claims": [],
    "reviewer_roles": list(ROLES),
    "stop_when_no_material_gain": True,
}
SAFE_OPS = ("scene_extend", "scene_shorten", "scene_shift", "caption_words")
SAFE_MAX_FRAMES = 60  # ≤ 2 с при 30 к/с: сдвиг или растяжение сцены, которое не меняет смысла
MAX_LIST = 200


class PolicyError(ValueError):
    pass


def _bool(data: dict, key: str, out: dict) -> None:
    if key in data:
        if not isinstance(data[key], bool):
            raise PolicyError(f"{key} — true или false.")
        out[key] = data[key]


def _num(data: dict, key: str, out: dict, lo: float, hi: float, *, integer: bool = False) -> None:
    if key not in data:
        return
    value = data[key]
    if isinstance(value, bool) or not isinstance(value, int | float) or not lo <= value <= hi:
        raise PolicyError(f"{key} — число от {lo:g} до {hi:g}.")
    if integer and int(value) != value:
        raise PolicyError(f"{key} — целое число.")
    out[key] = int(value) if integer else float(value)


def _ids(data: dict, key: str, out: dict) -> None:
    if key not in data:
        return
    value = data[key]
    if not isinstance(value, list) or len(value) > MAX_LIST or not all(isinstance(v, str) and v for v in value):
        raise PolicyError(f"{key} — список строк.")
    out[key] = list(dict.fromkeys(value))


def clean(payload: object, base: dict | None = None) -> dict:
    """Настройки от человека поверх base (или DEFAULT); неизвестные ключи игнорируются."""
    data = payload if isinstance(payload, dict) else {}
    out = {**DEFAULT, **(base or {})}
    _bool(data, "enabled", out)
    _num(data, "max_rounds", out, 1, 5, integer=True)
    if "mode" in data:
        if data["mode"] not in MODES:
            raise PolicyError(f"mode — один из: {', '.join(MODES)}.")
        out["mode"] = data["mode"]
    if "review_profile" in data:
        if data["review_profile"] not in PROFILES:
            raise PolicyError(f"review_profile — один из: {', '.join(PROFILES)}.")
        out["review_profile"] = data["review_profile"]
    _num(data, "quality_target", out, 0, 1)
    _num(data, "max_gpu_minutes", out, 0, 24 * 60)
    _num(data, "max_wall_time_minutes", out, 0, 24 * 60)
    _num(data, "max_new_assets", out, 0, 1000, integer=True)
    _num(data, "max_render_attempts_per_issue", out, 1, 5, integer=True)
    _bool(data, "allow_narrative_edits", out)
    _bool(data, "stop_when_no_material_gain", out)
    if "protected_ranges" in data:
        ranges = data["protected_ranges"]
        if not isinstance(ranges, list) or len(ranges) > MAX_LIST:
            raise PolicyError("protected_ranges — список {from, to} по индексам слов.")
        cleaned = []
        for item in ranges:
            if not isinstance(item, dict):
                raise PolicyError("protected_ranges — список {from, to}.")
            a, b = item.get("from"), item.get("to")
            ok = all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in (a, b))
            if not ok or b < a:
                raise PolicyError("protected_ranges: from ≤ to, целые индексы слов.")
            cleaned.append({"from": a, "to": b})
        out["protected_ranges"] = cleaned
    _ids(data, "locked_scenes", out)
    _ids(data, "protected_claims", out)
    if "reviewer_roles" in data:
        _ids(data, "reviewer_roles", out)
        unknown = [r for r in out["reviewer_roles"] if r not in ROLES]
        if unknown:
            raise PolicyError(f"Неизвестные роли: {', '.join(unknown)}. Доступны: {', '.join(ROLES)}.")
        if not out["reviewer_roles"]:
            raise PolicyError("Нужна хотя бы одна роль-ревьюер.")
    return out


def weights(policy: dict) -> dict[str, float]:
    return dict(PROFILES.get(policy.get("review_profile", "balanced"), PROFILES["balanced"]))


def is_safe(op: str, params: dict | None = None) -> bool:
    """Правка, которую auto_safe принимает без человека: не меняет смысл и короткая."""
    if op not in SAFE_OPS:
        return False
    frames = abs(int((params or {}).get("frames") or 0))
    return frames <= SAFE_MAX_FRAMES


def protected(policy: dict, *, scene_id: str | None = None, words: tuple[int, int] | None = None,
              claim_ids: tuple[str, ...] | list[str] = ()) -> str:  # fmt: skip
    """Почему действие нельзя: locked сцена, защищённые слова или утверждения; пусто — можно."""
    if scene_id and scene_id in policy.get("locked_scenes", []):
        return f"сцена {scene_id} заблокирована человеком"
    if words is not None:
        a, b = words
        for item in policy.get("protected_ranges", []):
            if a <= item["to"] and b >= item["from"]:
                return f"слова {a}–{b} в защищённом диапазоне {item['from']}–{item['to']}"
    hit = [c for c in claim_ids if c in policy.get("protected_claims", [])]
    if hit:
        return f"утверждение {hit[0]} защищено"
    return ""


def budget_left(policy: dict, used: dict) -> dict:
    """Остаток лимитов: секунды тяжёлых процессов, стенное время, новые ассеты; 0 → исчерпано."""
    gpu_limit = policy.get("max_gpu_minutes", 0) * 60
    wall_limit = policy.get("max_wall_time_minutes", 0) * 60
    return {
        "gpu_s": max(0.0, gpu_limit - used.get("gpu_s", 0.0)) if gpu_limit else None,
        "wall_s": max(0.0, wall_limit - used.get("wall_s", 0.0)) if wall_limit else None,
        "new_assets": max(0, policy.get("max_new_assets", 0) - used.get("new_assets", 0)),
    }


def exhausted(policy: dict, used: dict) -> str:
    """Какой лимит исчерпан (текст), или пусто."""
    left = budget_left(policy, used)
    if left["gpu_s"] is not None and left["gpu_s"] <= 0:
        return f"лимит тяжёлых процессов {policy['max_gpu_minutes']:g} мин исчерпан"
    if left["wall_s"] is not None and left["wall_s"] <= 0:
        return f"лимит времени {policy['max_wall_time_minutes']:g} мин исчерпан"
    if left["new_assets"] <= 0:
        return f"лимит новых ассетов {policy['max_new_assets']} исчерпан"
    return ""


def public() -> dict:
    """Для UI: режимы, профили с весами, роли, значения по умолчанию."""
    return {
        "modes": [{"id": k, "label": v} for k, v in MODES.items()],
        "profiles": [{"id": k, "weights": v} for k, v in PROFILES.items()],
        "roles": list(ROLES),
        "defaults": DEFAULT,
    }
