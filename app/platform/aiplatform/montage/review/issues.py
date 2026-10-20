"""Замечания ревью (ITERATIVE_REVIEW_SPEC §5–6): контракт, валидатор, дедуп, конфликты, арбитр, ранг, политика.

Замечание — схема `review_issue`; `make` собирает его из полей роли и даёт id `rev-<раунд>-<роль>-<n>`.
`validate` — диапазоны в пределах версии, сцены существуют, evidence не пуст, правка исполнима;
не прошло — статус `invalid`, в план не идёт. `dedup` склеивает одинаковую категорию по общей сцене или
пересечению диапазона ≥ OVERLAP. `conflicts` + `arbitrate` — противоположные правки одной сцены: выше
ранг → выше уверенность → дешевле; ничья — `deferred`, решает человек. `apply_policy` — защищённые области,
запрет правок речи, замечания без наблюдаемого evidence. `order` — ранжирование §6.3.
"""

from __future__ import annotations

from aiplatform.montage import schemas
from aiplatform.montage.review import policy as policy_module

SEVERITIES = ("blocker", "major", "minor", "suggestion")
RANKS = ("technical", "factual", "clarity", "aesthetic")
RISKS = ("low", "medium", "high")
OPS = ("scene_extend", "scene_shorten", "scene_shift", "scene_disable", "scene_props", "caption_words", "cut_words",
       "none")  # fmt: skip
OBSERVED = ("observed_video", "observed_audio")
OVERLAP = 0.5
OPPOSITE = {("scene_extend", "scene_shorten"), ("scene_shorten", "scene_extend")}
CATEGORY_RANK = {
    "qa_": "technical", "flash_safety": "technical", "clipping": "technical", "asset_": "technical",
    "av_sync": "technical", "caption_overflow": "technical",
    "factual": "factual", "grounding": "factual", "accuracy": "factual",
    "motion_timing": "clarity", "caption_speed": "clarity", "clarity": "clarity", "pacing": "clarity",
    "narrative": "clarity", "readability": "clarity", "contrast": "clarity", "hook": "clarity",
}  # fmt: skip
LIMITS = {"observation": 400, "impact": 300, "proposed_change": 300, "acceptance_check": 300}
MAX_PER_ROLE = 20


def rank_of(category: str, given: object = None) -> str:
    if given in RANKS:
        return str(given)
    for prefix, rank in CATEGORY_RANK.items():
        if category.startswith(prefix) or prefix.rstrip("_") == category:
            return rank
    return "aesthetic"


def _text(value: object, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _num(value: object, lo: float, hi: float, default: float) -> float:
    try:
        return min(hi, max(lo, float(value)))
    except (TypeError, ValueError):
        return default


def _int(value: object, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def clean_change(value: object) -> dict:
    data = value if isinstance(value, dict) else {}
    op = data.get("op") if data.get("op") in OPS else "none"
    out: dict = {"op": op}
    if op.startswith("scene_"):
        out["scene_id"] = str(data.get("scene_id") or "")
    if op in ("scene_extend", "scene_shorten", "scene_shift"):
        out["frames"] = _int(data.get("frames"), 30)
    if op == "scene_props":
        out["props"] = data.get("props") if isinstance(data.get("props"), dict) else {}
    if op == "caption_words":
        out["max_words"] = min(3, max(1, _int(data.get("max_words"), 2)))
    if op == "cut_words":
        out["from"], out["to"] = max(0, _int(data.get("from"))), max(0, _int(data.get("to")))
    return out


def clean_evidence(value: object) -> list[dict]:
    out = []
    for item in value if isinstance(value, list) else []:
        if (
            not isinstance(item, dict)
            or item.get("type")
            not in schemas.load("review_issue")["properties"]["evidence"]["items"]["properties"]["type"]["enum"]
        ):
            continue
        ev = {"type": item["type"]}
        for key in ("artifact_id", "doc", "value", "note"):
            if isinstance(item.get(key), str) and item[key]:
                ev[key] = item[key][:200]
        for key in ("frame", "frame_end", "chunk"):
            if isinstance(item.get(key), int) and not isinstance(item[key], bool) and item[key] >= 0:
                ev[key] = item[key]
        if isinstance(item.get("t"), int | float) and not isinstance(item["t"], bool) and item["t"] >= 0:
            ev["t"] = round(float(item["t"]), 3)
        words = item.get("words")
        if isinstance(words, dict) and all(isinstance(words.get(k), int) for k in ("from", "to")):
            ev["words"] = {"from": max(0, words["from"]), "to": max(0, words["to"])}
        out.append(ev)
    return out[:10]


def make(role: str, n: int, *, round_id: str, revision_id: str, category: str, severity: str, confidence: float,
         scene_ids: list[str], start_frame: int, end_frame: int, evidence: list[dict], observation: str,
         impact: str = "", proposed_change: str = "", acceptance_check: str = "", risk: str = "low",
         change: dict | None = None, cost: dict | None = None, rank: str | None = None,
         degraded: bool = False) -> dict:  # fmt: skip
    """Собрать замечание по контракту; evidence_type — самый сильный из улик."""
    evidence = clean_evidence(evidence)
    kinds = [e["type"] for e in evidence]
    strongest = next((k for k in ("observed_video", "observed_audio", "source_document", "transcript", "heuristic")
                      if k in kinds), "heuristic")  # fmt: skip
    severity = severity if severity in SEVERITIES else "minor"
    change = clean_change(change)
    item = {
        "issue_id": f"rev-{round_id[-3:]}-{role}-{n:03d}",
        "round_id": round_id,
        "revision_id": revision_id,
        "reviewer_role": role,
        "reviewer_roles": [role],
        "category": _text(category, 60) or "other",
        "rank": rank_of(category, rank),
        "severity": severity,
        "confidence": round(_num(confidence, 0, 1, 0.5), 2),
        "scene_ids": [s for s in scene_ids if isinstance(s, str)][:10],
        "start_frame": max(0, _int(start_frame)),
        "end_frame": max(0, _int(end_frame)),
        "evidence": evidence,
        "evidence_type": strongest,
        "observation": _text(observation, LIMITS["observation"]) or "(без текста)",
        "impact": _text(impact, LIMITS["impact"]),
        "proposed_change": _text(proposed_change, LIMITS["proposed_change"]),
        "acceptance_check": _text(acceptance_check, LIMITS["acceptance_check"]),
        "risk": risk if risk in RISKS else "medium",
        "estimated_cost": {
            "render_seconds": _num((cost or {}).get("render_seconds"), 0, 1e6, 0),
            "assets": max(0, _int((cost or {}).get("assets"))),
        },  # fmt: skip
        "status": "proposed",
        "status_reason": "",
        "change": change,
        "degraded": degraded,
        "fingerprint": "",
        "merged_from": [],
        "conflicts_with": [],
        "comments": [],
        "version": 1,
    }
    item["fingerprint"] = fingerprint(item)
    return item


def fingerprint(issue: dict) -> str:
    """Отпечаток «того же» замечания между раундами: категория, сцены (или «global») и правка — без кадров,
    которые после правок сдвигаются."""
    where = ",".join(sorted(issue["scene_ids"])) or "global"
    return f"{issue['category']}:{where}:{issue['change']['op']}"


def validate(issue: dict, *, duration_frames: int, scene_ids: set[str], n_words: int) -> list[str]:
    """Расхождения с контрактом и версией; пусто — годно."""
    out = schemas.validate("review_issue", issue)
    if issue["end_frame"] < issue["start_frame"]:
        out.append("end_frame раньше start_frame")
    if duration_frames and issue["start_frame"] > duration_frames:
        out.append(f"start_frame {issue['start_frame']} за концом версии ({duration_frames})")
    unknown = [s for s in issue["scene_ids"] if s not in scene_ids]
    if unknown:
        out.append(f"нет сцен: {', '.join(unknown)}")
    if not issue["evidence"]:
        out.append("нет evidence")
    change = issue["change"]
    if change["op"].startswith("scene_") and change.get("scene_id") not in scene_ids:
        out.append(f"правка ссылается на несуществующую сцену {change.get('scene_id')!r}")
    if change["op"] == "cut_words" and (change["to"] < change["from"] or change["to"] >= max(n_words, 1)):
        out.append("cut_words вне расшифровки")
    return out


def _overlap(a: dict, b: dict) -> float:
    lo, hi = max(a["start_frame"], b["start_frame"]), min(a["end_frame"], b["end_frame"])
    if hi <= lo:
        return 0.0
    shortest = min(a["end_frame"] - a["start_frame"], b["end_frame"] - b["start_frame"]) or 1
    return (hi - lo) / shortest


def _same(a: dict, b: dict) -> bool:
    if a["category"] != b["category"] or opposed(a, b):  # противоположные правки — конфликт, не дубль
        return False
    if set(a["scene_ids"]) & set(b["scene_ids"]):
        return True
    return not a["scene_ids"] and not b["scene_ids"] and _overlap(a, b) >= OVERLAP


def dedup(issues: list[dict]) -> list[dict]:
    """Одинаковые замечания разных ролей → одно: роли и evidence суммируются, уверенность — максимум."""
    out: list[dict] = []
    for issue in issues:
        if issue["status"] == "invalid":
            out.append(issue)
            continue
        twin = next((o for o in out if o["status"] != "invalid" and _same(o, issue)), None)
        if twin is None:
            out.append(issue)
            continue
        twin["reviewer_roles"] = list(dict.fromkeys([*twin["reviewer_roles"], *issue["reviewer_roles"]]))
        twin["evidence"] = (twin["evidence"] + [e for e in issue["evidence"] if e not in twin["evidence"]])[:10]
        twin["confidence"] = max(twin["confidence"], issue["confidence"])
        if SEVERITIES.index(issue["severity"]) < SEVERITIES.index(twin["severity"]):
            twin["severity"] = issue["severity"]
        if twin["change"]["op"] == "none" and issue["change"]["op"] != "none":
            twin["change"] = issue["change"]
            twin["fingerprint"] = fingerprint(twin)
        twin["degraded"] = twin["degraded"] and issue["degraded"]
        twin["merged_from"].append(issue["issue_id"])
        kinds = [e["type"] for e in twin["evidence"]]
        twin["evidence_type"] = next((k for k in ("observed_video", "observed_audio", "source_document", "transcript",
                                                  "heuristic") if k in kinds), twin["evidence_type"])  # fmt: skip
    return out


def opposed(a: dict, b: dict) -> bool:
    ca, cb = a["change"], b["change"]
    if ca["op"] == "none" or cb["op"] == "none" or ca.get("scene_id") != cb.get("scene_id") or not ca.get("scene_id"):
        return False
    if (ca["op"], cb["op"]) in OPPOSITE:
        return True
    if ca["op"] == cb["op"] == "scene_shift" and (ca["frames"] > 0) != (cb["frames"] > 0):
        return True
    return "scene_disable" in (ca["op"], cb["op"]) and ca["op"] != cb["op"]


def conflicts(issues: list[dict]) -> list[tuple[dict, dict]]:
    live = [i for i in issues if i["status"] in ("proposed", "accepted")]
    pairs = []
    for n, a in enumerate(live):
        for b in live[n + 1 :]:
            if opposed(a, b):
                pairs.append((a, b))
                a["conflicts_with"] = list(dict.fromkeys([*a["conflicts_with"], b["issue_id"]]))
                b["conflicts_with"] = list(dict.fromkeys([*b["conflicts_with"], a["issue_id"]]))
    return pairs


def order_key(issue: dict) -> tuple:
    """Ранжирование §6.3: ранг → серьёзность → уверенность → цена → риск."""
    return (RANKS.index(issue["rank"]), SEVERITIES.index(issue["severity"]), -issue["confidence"],
            issue["estimated_cost"]["render_seconds"], RISKS.index(issue["risk"]))  # fmt: skip


def arbitrate(issues: list[dict]) -> list[dict]:
    """Конфликты: побеждает лучший по order_key; ничья по рангу и серьёзности с близкой уверенностью —
    оба deferred, решает человек. Возвращает записи решений для журнала."""
    log = []
    for a, b in conflicts(issues):
        if "deferred" in (a["status"], b["status"]) or "rejected" in (a["status"], b["status"]):
            continue
        ka, kb = order_key(a), order_key(b)
        tie = ka[:2] == kb[:2] and abs(a["confidence"] - b["confidence"]) < 0.15
        if tie:
            for item in (a, b):
                item["status"] = "deferred"
                item["status_reason"] = (
                    f"арбитр: конфликт с {b['issue_id'] if item is a else a['issue_id']} — решает человек"
                )
            log.append({"winner": None, "pair": [a["issue_id"], b["issue_id"]], "rationale": "ничья"})
            continue
        winner, loser = (a, b) if ka < kb else (b, a)
        loser["status"] = "rejected"
        loser["status_reason"] = f"арбитр: проиграло {winner['issue_id']} ({winner['rank']}, {winner['severity']})"
        log.append({"winner": winner["issue_id"], "pair": [a["issue_id"], b["issue_id"]],
                    "rationale": f"{winner['rank']}/{winner['severity']}/{winner['confidence']} против "
                                 f"{loser['rank']}/{loser['severity']}/{loser['confidence']}"})  # fmt: skip
    return log


def apply_policy(issues: list[dict], policy: dict, *, scenes: dict[str, dict]) -> None:
    """Защищённые области, запрет правок речи: такие замечания rejected с причиной (§6.3, §7)."""
    for issue in issues:
        if issue["status"] != "proposed":
            continue
        change = issue["change"]
        scene = scenes.get(change.get("scene_id") or "")
        words = (change["from"], change["to"]) if change["op"] == "cut_words" else None
        why = policy_module.protected(policy, scene_id=change.get("scene_id"), words=words,
                                      claim_ids=(scene or {}).get("grounding_claim_ids") or [])  # fmt: skip
        if why and change["op"] != "none":
            issue["status"], issue["status_reason"] = "rejected", f"политика: {why}"
        elif change["op"] == "cut_words" and not policy.get("allow_narrative_edits"):
            issue["status"], issue["status_reason"] = (
                "rejected",
                "политика: правки речи запрещены (allow_narrative_edits)",
            )


def needs_approval(issue: dict) -> bool:
    """Замечание только с эвристической уликой (ни замера, ни слов, ни документа) не запускает
    перегенерацию само (§4): только через человека. Тайминг по словам бита — проверяемая улика."""
    return issue["evidence_type"] == "heuristic" and issue["change"]["op"] != "none"


def counts(issues: list[dict]) -> dict:
    out = {k: 0 for k in ("open", "accepted", "rejected", "deferred", "fixed", "failed", "invalid", "total")}
    for issue in issues:
        out["total"] += 1
        status = issue["status"]
        if status == "proposed":
            out["open"] += 1
        elif status in out:
            out[status] += 1
        elif status == "regressed":
            out["failed"] += 1
    return out


def by_id(issues: list[dict]) -> dict[str, dict]:
    return {i["issue_id"]: i for i in issues}
