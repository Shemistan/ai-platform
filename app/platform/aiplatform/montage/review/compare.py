"""Regression gate (ITERATIVE_REVIEW_SPEC §8): baseline Vn против кандидата Vn+1 — три слоя раздельно.

Hard gates: файл читается ffprobe; длительность = таймлайну (±1 кадр + хвост loudnorm); AV sync (видео
и звук одной длины ±1,5 кадра); новых механических ошибок QA нет; защищённые слова остались в тех же
сегментах; locked сцены с теми же хэшами; размеры исходников те же. Objective: счётчики QA,
переполнения субтитров, упавшие/заблокированные сцены, дрейф длительности — «хуже/лучше» по каждому.
Subjective: судья-LLM (`montage_review_judge`) оценивает пары A/B по измерениям профиля с evidence, без
самооценки исполнителя; без LLM — `human_review`. Решение: hard fail → rejected; objective хуже →
rejected; взвешенный дельта > GAIN или закрыто ≥ 1 замечание без ухудшений → accepted; иначе ambiguous.
"""

from __future__ import annotations

import json
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import media
from aiplatform.montage import qa, schemas
from aiplatform.montage.review import policy as policy_module
from aiplatform.montage.review import versions

GAIN = 0.05
JUDGE_RULES = (
    "Ты — независимый судья двух версий одного ролика: A (baseline) и B (кандидат после правок). Тебе даны "
    "манифесты обеих версий, список замечаний, которые правки должны были закрыть, и измерения. Оцени каждое "
    "измерение для A и B по шкале 1–5 и приведи evidence — конкретные сцены, кадры или слова, а не общие слова. "
    "Не доверяй описанию правок: смотри, что изменилось в манифестах. Если разницы нет — одинаковые баллы."
)


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _streams(path: Path, ffmpeg: str) -> dict:
    argv = [media.ffprobe_for(ffmpeg), "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "json",
            str(path)]  # fmt: skip
    try:
        info = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    out = {}
    for s in info.get("streams") or []:
        try:
            out[s["codec_type"]] = float(s.get("duration") or 0)
        except (TypeError, ValueError):
            continue
    return out


def hard_gates(
    root: Path, base: int, cand: int, *, policy: dict, ffmpeg: str, source_sizes: dict[str, int]
) -> list[dict]:
    """Список {id, ok, detail} — все проверки, а не первая упавшая."""
    cdir, bdir = versions.folder_of(root, cand), versions.folder_of(root, base)
    out: list[dict] = []
    video = cdir / versions.OUTPUT
    info = qa.probe_streams(video, ffmpeg)
    out.append({"id": "file", "ok": bool(info and info.get("video") and info.get("audio")),
                "detail": "ffprobe: видео и звук на месте" if info else "ffprobe не читает файл"})  # fmt: skip
    timeline = _read(cdir / "timeline.json")
    fps = timeline.get("fps") or 30
    expected = (timeline.get("duration_frames") or 0) / fps
    drift = (info.get("duration") or 0) - expected
    ok = bool(info) and -1.5 / fps <= drift <= 1.5 / fps + qa.LOUDNORM_TAIL_S
    out.append(
        {"id": "duration", "ok": ok, "detail": f"{info.get('duration', 0):.2f} с против {expected:.2f} с по таймлайну"}
    )
    streams = _streams(video, ffmpeg)
    if "video" in streams and "audio" in streams:
        gap = abs(streams["video"] - streams["audio"])
        out.append(
            {
                "id": "av_sync",
                "ok": gap <= 1.5 / fps + qa.LOUDNORM_TAIL_S,
                "detail": f"дорожки расходятся на {gap:.3f} с",
            }
        )
    else:
        out.append({"id": "av_sync", "ok": False, "detail": "нет длительности дорожек"})
    errors_b = {i["check"] for i in (_read(bdir / "qa_report.json").get("items") or []) if i["severity"] == "error"}
    errors_c = {i["check"] for i in (_read(cdir / "qa_report.json").get("items") or []) if i["severity"] == "error"}
    new = sorted(errors_c - errors_b)
    out.append(
        {
            "id": "no_new_blockers",
            "ok": not new,
            "detail": "новых ошибок QA нет" if not new else "новые ошибки: " + ", ".join(new),
        }
    )
    out.append(_protected_gate(bdir, cdir, policy))
    out.append(_locked_gate(bdir, cdir, policy))
    manifest = _read(root / "source_manifest.json")
    bad = [s["role"] for s in manifest.get("sources") or [] if source_sizes.get(s["role"]) != s.get("size")]
    out.append(
        {
            "id": "sources",
            "ok": not bad,
            "detail": "исходники не менялись" if not bad else "изменились: " + ", ".join(bad),
        }
    )
    return out


def _words_in(version_dir: Path, lo: int, hi: int) -> list[int]:
    words = (_read(version_dir / "transcript_edit.json")).get("words") or []
    return [w["src"] for w in words if lo <= w.get("src", -1) <= hi]


def _protected_gate(bdir: Path, cdir: Path, policy: dict) -> dict:
    ranges = policy.get("protected_ranges") or []
    for item in ranges:
        if _words_in(bdir, item["from"], item["to"]) != _words_in(cdir, item["from"], item["to"]):
            return {"id": "protected_ranges", "ok": False, "detail": f"слова {item['from']}–{item['to']} изменились"}
    return {"id": "protected_ranges", "ok": True, "detail": f"защищённых диапазонов: {len(ranges)}, целы"}


def _locked_gate(bdir: Path, cdir: Path, policy: dict) -> dict:
    locked = policy.get("locked_scenes") or []
    sb = {s["id"]: s for s in (_read(bdir / "storyboard.json")).get("scenes") or []}
    sc = {s["id"]: s for s in (_read(cdir / "storyboard.json")).get("scenes") or []}
    for sid in locked:
        a, b = sb.get(sid), sc.get(sid)
        if a and (not b or a["hash"] != b["hash"] or a.get("enabled", True) != b.get("enabled", True)
                  or a["edit_start_frame"] != b["edit_start_frame"]):  # fmt: skip
            return {"id": "locked_scenes", "ok": False, "detail": f"{sid} изменена"}
    return {"id": "locked_scenes", "ok": True, "detail": f"заблокированных сцен: {len(locked)}, целы"}


def objective(root: Path, base: int, cand: int) -> dict:
    """Метрики по QA и манифестам обеих версий; worse/better — по каждой (меньше — лучше)."""

    def metrics(n: int) -> dict:
        vdir = versions.folder_of(root, n)
        report = _read(vdir / "qa_report.json")
        items = report.get("items") or []
        scenes = (_read(vdir / "storyboard.json")).get("scenes") or []
        record = versions.get(root, n) or {}
        timeline = _read(vdir / "timeline.json")
        fps = timeline.get("fps") or 30
        return {
            "qa_errors": sum(1 for i in items if i["severity"] == "error" and i["kind"] == "mechanical"),
            "qa_warnings": sum(1 for i in items if i["severity"] == "warning"),
            "caption_overflow": sum(1 for i in items if i["check"] == "caption_overflow"),
            "scenes_failed": sum(1 for s in scenes if s.get("enabled", True) and s["status"] in ("failed", "blocked")),
            "assets_missing": sum(1 for i in items if i["check"] == "asset_missing"),
            "clipping": sum(1 for i in items if i["check"] == "clipping"),
            "duration_drift_frames": abs(
                round(((record.get("output") or {}).get("duration") or 0) * fps)
                - (timeline.get("duration_frames") or 0)
            ),  # fmt: skip
        }

    a, b = metrics(base), metrics(cand)
    tail = round(qa.LOUDNORM_TAIL_S * 30)
    worse = [k for k in a if b[k] > a[k] and (k != "duration_drift_frames" or b[k] > tail)]
    better = [k for k in a if b[k] < a[k]]
    return {"baseline": a, "candidate": b, "worse": worse, "better": better}


def judge_prompt(texts_a: dict, texts_b: dict, issues: list[dict], diff: dict, weights: dict) -> str:
    fixes = "\n".join(f"- {i['issue_id']} ({i['category']}): {i['observation']} → проверка: {i['acceptance_check']}"
                      for i in issues) or "- (правок не было)"  # fmt: skip
    dims = ", ".join(f"{k} (вес {v})" for k, v in weights.items())
    return (
        f"### task: montage_review_judge\n{JUDGE_RULES}\nИзмерения: {dims}.\n"
        'Ответь только JSON: {"items": [{"dimension": "<имя>", "a": <1–5>, "b": <1–5>, "evidence": "<что именно>"}], '
        '"summary": "<одна фраза>"}\n\n'
        f"Замечания, которые должны быть закрыты:\n{fixes}\n\nЧто изменилось по манифестам: {json.dumps(diff, ensure_ascii=False)}\n\n"
        f"A — сцены:\n{texts_a['scenes']}\nA — QA:\n{texts_a['qa']}\n\n"
        f"B — сцены:\n{texts_b['scenes']}\nB — QA:\n{texts_b['qa']}\n\nРасшифровка B:\n{texts_b['transcript']}\n"
    )


def subjective(policy: dict, ask: Callable[[str], tuple[object, str, str | None]] | None, prompt: str) -> dict:
    weights = policy_module.weights(policy)
    if ask is None:
        return {"mode": "human_review", "items": [], "profile": policy["review_profile"], "weighted_delta": None,
                "model": "", "prompt_version": None, "summary": "LLM нет — субъективную часть оценивает человек"}  # fmt: skip
    data, model, version = ask(prompt)
    items = []
    for raw in (data.get("items") if isinstance(data, dict) else None) or []:
        if not isinstance(raw, dict) or raw.get("dimension") not in weights:
            continue
        try:
            a, b = min(5.0, max(1.0, float(raw.get("a")))), min(5.0, max(1.0, float(raw.get("b"))))
        except (TypeError, ValueError):
            continue
        items.append({"dimension": raw["dimension"], "a": a, "b": b, "evidence": str(raw.get("evidence") or "")[:300],
                      "weight": weights[raw["dimension"]]})  # fmt: skip
    delta = round(sum(i["weight"] * (i["b"] - i["a"]) / 4 for i in items), 3) if items else 0.0
    return {"mode": "judge", "items": items, "profile": policy["review_profile"], "weighted_delta": delta,
            "model": model, "prompt_version": version,
            "summary": str((data.get("summary") if isinstance(data, dict) else "") or "")[:300]}  # fmt: skip


IDENTICAL = "ролик не изменился: sha256 кандидата равен baseline"


def decide(
    hard: list[dict], obj: dict, subj: dict, *, fixed: list[str], regressed: list[str], policy: dict,
    identical: bool = False,
) -> tuple[str, str, str]:  # fmt: skip
    """(decision, decided_by, reason) по §8. identical — файл кандидата байт в байт как baseline: это не
    улучшение, что бы ни сказал судья (§1.5: не объявлять улучшение без измерений)."""
    failed = [g["id"] for g in hard if not g["ok"]]
    if failed:
        return "rejected", "gates", "hard gates: " + ", ".join(failed)
    if identical:
        return "rejected", "gates", IDENTICAL
    if obj["worse"]:
        return "rejected", "gates", "объективно хуже: " + ", ".join(obj["worse"])
    if regressed and not fixed:
        return "rejected", "gates", "замечания вернулись: " + ", ".join(regressed[:3])
    delta = subj.get("weighted_delta")
    if subj["mode"] == "judge" and delta is not None:
        if delta < -GAIN:
            return "rejected", "judge", f"судья: хуже на {delta:+.2f}"
        if delta > GAIN or (fixed and delta >= 0):
            return "accepted", "judge", f"судья: {delta:+.2f}, закрыто замечаний {len(fixed)}"
        if fixed and delta >= -GAIN:
            return "ambiguous", "judge", f"судья не видит выигрыша ({delta:+.2f}), но закрыто {len(fixed)}"
        return "ambiguous", "judge", "ни выигрыша, ни закрытых замечаний"
    if fixed and obj["better"]:
        return "accepted", "gates", "объективно лучше: " + ", ".join(obj["better"])
    if fixed:
        return "ambiguous", "pending", f"закрыто {len(fixed)}, субъективная оценка — за человеком"
    return "ambiguous", "pending", "ничего не закрыто и не измерено"


def record(round_id: str, base: str, cand: str, hard: list[dict], obj: dict, subj: dict, *, fixed: list[str],
           regressed: list[str], decision: str, decided_by: str, reason: str) -> dict:  # fmt: skip
    data = {
        "round_id": round_id, "baseline": base, "candidate": cand, "made_at": round(time.time(), 3),
        "hard_gates": hard, "hard_ok": all(g["ok"] for g in hard), "objective": obj, "subjective": subj,
        "fixed_issues": fixed, "regressed_issues": regressed, "decision": decision, "decided_by": decided_by,
        "reason": reason,
    }  # fmt: skip
    schemas.check("review_comparison", data)
    return data
