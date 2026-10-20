"""План исправлений (ITERATIVE_REVIEW_SPEC §6.4–6.7): действия из замечаний, DAG, impact, оценка, политика.

Замечание с исполнимой правкой → действие (`repair_action`). Зависимости: правки речи (`cut_words`) —
раньше сценных, действия одной сцены — по очереди. Impact — кадры сцены до и после правки, хэш сцены до.
Оценка — секунды рендера (перерисовка сцены ≈ RENDER_FACTOR × её длительность, финальный проход —
FINAL_FACTOR × ролик, один на план) и новые ассеты (перерисованные сцены). Утверждение по режиму:
manual_approval — всё; auto_safe — небезопасное; autonomous — смысловые удаления, правки речи, защищённые
утверждения и замечания без наблюдаемого evidence. Бюджет: сверх лимита — deferred с причиной.
Осцилляция — обратное действие по той же сцене, что в прошлом раунде, отклоняется.
"""

from __future__ import annotations

import time

from aiplatform.montage import schemas, templates
from aiplatform.montage.review import issues as issues_module
from aiplatform.montage.review import policy as policy_module

RENDER_FACTOR = 3.0
FINAL_FACTOR = 0.3
RERENDER_OPS = ("scene_extend", "scene_shorten", "scene_props")
UNSAFE_AUTONOMOUS = ("scene_disable", "cut_words")
REVERSE = {"scene_extend": "scene_shorten", "scene_shorten": "scene_extend"}


def _frames_after(scene: dict, change: dict, fps: int, total: int) -> tuple[int, int]:
    start, dur = scene["edit_start_frame"], scene["duration_frames"]
    op, frames = change["op"], change.get("frames", 0)
    max_dur = round(templates.MAX_S * fps)
    min_dur = round(templates.MIN_S * fps)
    if op == "scene_extend":
        dur = min(max_dur, dur + abs(frames), max(min_dur, total - start))
    elif op == "scene_shorten":
        dur = max(min_dur, dur - abs(frames))
    elif op == "scene_shift":
        start = max(0, min(total - dur, start + frames))
    return start, dur


def action_for(issue: dict, n: int, *, scenes: dict[str, dict], fps: int, total: int, duration_s: float,
               revision_id: str) -> dict | None:  # fmt: skip
    change = issue["change"]
    op = change["op"]
    if op == "none":
        return None
    scene = scenes.get(change.get("scene_id") or "")
    params = {k: v for k, v in change.items() if k not in ("op", "scene_id")}
    affected: list[dict] = []
    estimate = {"render_s": 0.0, "assets": 0}
    before = None
    if scene is not None:
        before = scene["hash"]
        affected.append({"from": scene["edit_start_frame"], "to": scene["edit_start_frame"] + scene["duration_frames"]})
        start, dur = _frames_after(scene, change, fps, total)
        affected.append({"from": start, "to": start + dur})
        if op in RERENDER_OPS:
            estimate = {"render_s": round(RENDER_FACTOR * dur / fps, 1), "assets": 1}
        if op in ("scene_extend", "scene_shorten"):
            params["frames"] = abs(dur - scene["duration_frames"])
        if op == "scene_shift":
            params["frames"] = start - scene["edit_start_frame"]
    elif op == "caption_words":
        affected.append({"from": 0, "to": total})
    elif op == "cut_words":
        affected.append({"from": 0, "to": total})
        estimate = {"render_s": round(FINAL_FACTOR * duration_s, 1), "assets": 0}
    return {
        "action_id": f"act-{n:03d}",
        "issue_ids": [issue["issue_id"]],
        "op": op,
        "scene_id": change.get("scene_id") if scene is not None else None,
        "params": params,
        "status": "proposed",
        "status_reason": "",
        "depends_on": [],
        "affected_frames": affected,
        "asset_hash_before": before,
        "asset_hash_after": None,
        "estimate": estimate,
        "approval": "auto",
        "safe": policy_module.is_safe(op, params),
        "test": issue["acceptance_check"] or issue["proposed_change"],
        "rollback": revision_id,
        "attempts": 0,
        "error": None,
        "fingerprint": f"{change.get('scene_id') or 'global'}:{op}",
    }


def plan(issues: list[dict], *, policy: dict, scenes: list[dict], fps: int, total_frames: int, duration_s: float,
         round_id: str, revision_id: str, budget_used: dict, previous_fingerprints: set[str]) -> dict:  # fmt: skip
    """RepairPlan по замечаниям proposed/accepted, в порядке ранга; политика и бюджет внутри."""
    by_id = {s["id"]: s for s in scenes}
    notes: list[str] = []
    actions: list[dict] = []
    seen_scene: dict[str, str] = {}
    cut_actions: list[str] = []
    live = sorted((i for i in issues if i["status"] in ("proposed", "accepted")), key=issues_module.order_key)
    for issue in live:
        action = action_for(issue, len(actions) + 1, scenes=by_id, fps=fps, total=total_frames,
                            duration_s=duration_s, revision_id=revision_id)  # fmt: skip
        if action is None:
            continue
        reverse = REVERSE.get(action["op"])
        if reverse and f"{action['scene_id']}:{reverse}" in previous_fingerprints:
            issue["status"], issue["status_reason"] = (
                "rejected",
                "осцилляция: обратное действие по этой сцене было в прошлом раунде",
            )
            notes.append(f"{issue['issue_id']}: осцилляция")
            continue
        twin = next((a for a in actions if a["fingerprint"] == action["fingerprint"]), None)
        if twin is not None:  # одно действие на несколько замечаний: короче субтитры просят две роли
            twin["issue_ids"] = list(dict.fromkeys([*twin["issue_ids"], *action["issue_ids"]]))
            twin["params"] = _merge_params(twin["op"], twin["params"], action["params"])
            twin["approval"] = _approval(issue, twin, policy) if twin["approval"] == "auto" else twin["approval"]
            continue
        if action["scene_id"] and action["scene_id"] in seen_scene:
            action["depends_on"].append(seen_scene[action["scene_id"]])
        if action["op"] == "cut_words":
            cut_actions.append(action["action_id"])
        elif cut_actions:
            action["depends_on"] = list(dict.fromkeys([*action["depends_on"], *cut_actions]))
        if action["scene_id"]:
            seen_scene[action["scene_id"]] = action["action_id"]
        action["approval"] = _approval(issue, action, policy)
        actions.append(action)
    _budget(actions, policy, budget_used, duration_s, notes)
    index = issues_module.by_id(issues)
    for action in actions:
        linked = [index[i] for i in action["issue_ids"] if i in index]
        if action["status"] == "proposed" and action["approval"] == "auto":
            action["status"] = "approved"
            for issue in linked:
                if issue["status"] == "proposed":
                    issue["status"] = "accepted"
        elif (
            action["status"] == "proposed" and action["approval"] == "required" and policy["mode"] != "manual_approval"
        ):
            # Автономия не стоит из-за правок, которые ждут человека: они откладываются, остальное идёт.
            action["status"], action["status_reason"] = "deferred", "нужно утверждение человека"
            notes.append(f"{action['action_id']}: ждёт человека")
    pending = [a for a in actions if a["approval"] == "required" and a["status"] in ("proposed", "deferred")]
    approved = [a for a in actions if a["status"] == "approved"]
    approval = "required" if pending and (policy["mode"] == "manual_approval" or not approved) else "not_needed"
    if approval == "required":
        for action in pending:
            action["status"], action["status_reason"] = "proposed", ""
    estimate = {
        "render_s": round(
            sum(a["estimate"]["render_s"] for a in actions if a["status"] != "deferred")
            + (FINAL_FACTOR * duration_s if actions else 0.0),
            1,
        ),  # fmt: skip
        "assets": sum(a["estimate"]["assets"] for a in actions if a["status"] != "deferred"),
        "within_budget": not any(
            a["status"] == "deferred" and a["status_reason"].startswith("бюджет") for a in actions
        ),
        "reason": "; ".join(notes)[:300],
    }
    data = {
        "plan_id": f"plan-{round_id[-3:]}",
        "round_id": round_id,
        "revision_id": revision_id,
        "made_at": round(time.time(), 3),
        "actions": actions,
        "estimate": estimate,
        "approval": approval,
        "approved_at": None,
        "approved_by": "",
        "policy": {
            "mode": policy["mode"],
            "rejected": sum(1 for i in issues if i["status"] == "rejected"),
            "deferred": sum(1 for i in issues if i["status"] == "deferred"),
            "notes": notes[:20],
        },  # fmt: skip
    }
    schemas.check("repair_plan", data)
    return data


def _merge_params(op: str, a: dict, b: dict) -> dict:
    out = {**a}
    if op in ("scene_extend", "scene_shorten", "scene_shift"):
        out["frames"] = max(a.get("frames", 0), b.get("frames", 0), key=abs)
    if op == "caption_words":
        out["max_words"] = min(a.get("max_words", 3), b.get("max_words", 3))
    if op == "scene_props":
        out["props"] = {**(a.get("props") or {}), **(b.get("props") or {})}
    return out


def _approval(issue: dict, action: dict, policy: dict) -> str:
    mode = policy["mode"]
    if mode == "manual_approval":
        return "required"
    if mode == "auto_safe":
        return "auto" if action["safe"] and not issues_module.needs_approval(issue) else "required"
    # autonomous: смысловые удаления, правки речи, защищённые утверждения, замечания без наблюдений — ворота (§2)
    if action["op"] in UNSAFE_AUTONOMOUS or issues_module.needs_approval(issue):
        return "required"
    return "auto"


def _budget(actions: list[dict], policy: dict, used: dict, duration_s: float, notes: list[str]) -> None:
    left = policy_module.budget_left(policy, used)
    assets = 0
    render_s = FINAL_FACTOR * duration_s
    for action in actions:
        assets_after = assets + action["estimate"]["assets"]
        render_after = render_s + action["estimate"]["render_s"]
        if assets_after > left["new_assets"]:
            action["status"], action["status_reason"] = (
                "deferred",
                f"бюджет: новых ассетов больше {policy['max_new_assets']}",
            )
            notes.append(f"{action['action_id']}: бюджет ассетов")
            continue
        if left["gpu_s"] is not None and render_after > left["gpu_s"]:
            action["status"], action["status_reason"] = "deferred", "бюджет: минут тяжёлых процессов не хватит"
            notes.append(f"{action['action_id']}: бюджет времени")
            continue
        assets, render_s = assets_after, render_after


def approve(
    plan_data: dict, issues: list[dict], *, decisions: dict[str, str], approve_all: bool, by: str = "human"
) -> None:
    """Решения человека: по замечаниям (accepted/rejected/deferred) и утверждение плана целиком."""
    index = issues_module.by_id(issues)
    for issue_id, status in decisions.items():
        issue = index.get(issue_id)
        if issue is None or status not in ("accepted", "rejected", "deferred"):
            continue
        issue["status"], issue["status_reason"] = status, f"решение человека: {status}"
        issue["version"] += 1
    for action in plan_data["actions"]:
        issue = index.get(action["issue_ids"][0])
        if issue is None:
            continue
        if issue["status"] == "rejected":
            action["status"], action["status_reason"], action["approval"] = "rejected", "отклонено человеком", "denied"
        elif issue["status"] == "deferred":
            action["status"], action["status_reason"] = "deferred", "отложено человеком"
        elif action["status"] in ("proposed", "deferred") and (approve_all or issue["status"] == "accepted"):
            if action["status_reason"].startswith("бюджет"):
                continue
            action["status"], action["approval"] = "approved", "given"
            if issue["status"] == "proposed":
                issue["status"] = "accepted"
    pending = any(a["status"] == "proposed" and a["approval"] == "required" for a in plan_data["actions"])
    if approve_all or not pending:
        plan_data["approval"] = "given" if any(a["status"] == "approved" for a in plan_data["actions"]) else "denied"
        plan_data["approved_at"] = round(time.time(), 3)
        plan_data["approved_by"] = by


def ready(plan_data: dict) -> list[dict]:
    """Утверждённые действия в порядке зависимостей (DAG → топологический порядок)."""
    approved = {a["action_id"]: a for a in plan_data["actions"] if a["status"] == "approved"}
    out: list[dict] = []
    seen: set[str] = set()

    def visit(action: dict) -> None:
        if action["action_id"] in seen:
            return
        seen.add(action["action_id"])
        for dep in action["depends_on"]:
            if dep in approved:
                visit(approved[dep])
        out.append(action)

    for action in approved.values():
        visit(action)
    return out


def scene_patch(action: dict, scene: dict, fps: int, total: int) -> dict:
    """Правка сцены для storyboard.edit по действию."""
    op, params = action["op"], action["params"]
    if op == "scene_disable":
        return {"id": scene["id"], "enabled": False}
    if op == "scene_props":
        return {"id": scene["id"], "props": {**scene["props"], **(params.get("props") or {})}}
    start, dur = _frames_after(scene, {"op": op, **params}, fps, total)
    return {"id": scene["id"], "edit_start_frame": start, "duration_frames": dur}
