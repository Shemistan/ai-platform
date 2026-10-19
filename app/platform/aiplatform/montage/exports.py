"""Экспорт (MASTER_SPEC §5.7–5.8): пресеты, provenance, архив проекта.

`exports/<n>/`: `export.<ext>`, `provenance.json` (источники с sha256, модели, версии промптов,
сцены с хэшами, ревизия стиля, ворота QA, причина override), `project.zip` (манифесты и субтитры,
без медиа). Пресет по умолчанию — YouTube 1080p H.264/AAC: результат уже в нём, поэтому поток
копируется; вертикаль 9:16 — центр-кроп; ProRes — мастер для монтажки.
"""

from __future__ import annotations

import json
import time
import zipfile
from pathlib import Path

import aiplatform
from aiplatform.montage import decisions, render, schemas, templates

DIR = "exports"
DEFAULT = "youtube_1080p"
PRESETS = {
    "youtube_1080p": {"id": "youtube_1080p", "label": "YouTube 1080p (H.264/AAC)", "container": "mp4",
                      "vcodec": "libx264", "acodec": "aac", "width": 1920, "height": 1080, "fps": render.FPS, "crf": 18,
                      "aspect": "16:9", "note": "результат сборки уже в этом формате — поток копируется"},
    "vertical_1080x1920": {"id": "vertical_1080x1920", "label": "Вертикаль 9:16 (центр-кроп)", "container": "mp4",
                           "vcodec": "libx264", "acodec": "aac", "width": 1080, "height": 1920, "fps": render.FPS,
                           "crf": 20, "aspect": "9:16", "note": "кадр режется по центру; для шортсов без переразметки"},
    "prores_master": {"id": "prores_master", "label": "ProRes 422 HQ (мастер)", "container": "mov",
                      "vcodec": "prores_ks", "acodec": "pcm_s16le", "width": 1920, "height": 1080, "fps": render.FPS,
                      "crf": None, "aspect": "16:9", "note": "для монтажки; файл большой"},
}  # fmt: skip
MANIFESTS = ("project.json", "plan.json", "edl.json", "timemap.json", "source_manifest.json", "documents.json",
             "knowledge.json", "beats.json", "style_bible.json", "storyboard.json", "timeline.json", "qa_report.json",
             "subtitles.srt", "subtitles.vtt", "decisions.jsonl", "transcript_edit.json")  # fmt: skip


def preset(pid: object) -> dict:
    if pid not in PRESETS:
        raise ValueError(f"Пресет — один из: {', '.join(PRESETS)}.")
    return PRESETS[pid]


def argv(preset_id: str, ffmpeg: str, src: Path, out: Path) -> list[str]:
    p = preset(preset_id)
    head = [ffmpeg, "-y", "-loglevel", "error", "-i", str(src)]
    if preset_id == "youtube_1080p":
        return [*head, "-c", "copy", "-movflags", "+faststart", str(out)]
    if preset_id == "vertical_1080x1920":
        vf = f"crop=ih*9/16:ih,scale={p['width']}:{p['height']},setsar=1"
        video = ["-c:v", "libx264", "-preset", "veryfast", "-crf", str(p["crf"]), "-pix_fmt", "yuv420p"]
        return [*head, "-vf", vf, *video, "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)]
    return [*head, "-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le", "-c:a", "pcm_s16le", str(out)]


def next_number(folder: Path) -> int:
    root = folder / DIR
    numbers = [int(p.name) for p in root.glob("*") if p.is_dir() and p.name.isdigit()] if root.is_dir() else []
    return max(numbers, default=0) + 1


def provenance(project: dict, folder: Path, preset_id: str, out: Path, *, override: str = "") -> dict:
    """Что и из чего собрано (§6, §10): хэши, модели, версии промптов, сцены, ворота QA."""

    def read(name: str) -> dict:
        try:
            return json.loads((folder / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    log = decisions.read(folder)
    llm_versions = sorted({d["prompt_version"] for d in log if d.get("prompt_version")})
    models = sorted({d["model"] for d in log if d.get("model")})
    board = read("storyboard.json").get("scenes") or []
    qa = read("qa_report.json")
    data = {
        "schema_version": schemas.SCHEMA_VERSION,
        "made_at": round(time.time(), 3),
        "platform_version": aiplatform.__version__,
        "template_version": templates.TEMPLATE_VERSION,
        "project_id": project["id"],
        "title": project["title"],
        "mode": project["options"]["mode"],
        "preset": preset(preset_id),
        "output": {"file": out.name, "sha256": decisions.file_sha(out), "size": out.stat().st_size},
        "sources": read("source_manifest.json").get("sources") or [],
        "documents": [
            {k: d.get(k) for k in ("id", "name", "sha256", "status")}
            for d in read("documents.json").get("documents") or []
        ],
        "transcript": {**(project.get("transcript") or {}), "asr_model": project["options"]["model"]},
        "llm": {"models": models, "prompt_versions": llm_versions, "decisions": len(log)},
        "cuts": (project.get("plan") or {}).get("cuts"),
        "style_revision": read("style_bible.json").get("revision"),
        "scenes": [
            {
                "id": s["id"],
                "template_id": s["template_id"],
                "hash": s["hash"],
                "status": s["status"],
                "edit_start_frame": s["edit_start_frame"],
                "duration_frames": s["duration_frames"],
            }
            for s in board
            if s.get("enabled", True)
        ],  # fmt: skip
        "qa": {"gates": qa.get("gates"), "counts": qa.get("counts"), "repair_attempts": qa.get("repair_attempts")},
        "override_reason": override or None,
        "approvals": project.get("approvals"),
    }
    return data


def archive(folder: Path, out: Path) -> Path:
    """zip манифестов проекта (без медиа) — чтобы проект можно было прочитать и воспроизвести."""
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in MANIFESTS:
            path = folder / name
            if path.is_file():
                zf.write(path, name)
        for path in sorted((folder / "scenes").glob("*/scene.json")) if (folder / "scenes").is_dir() else []:
            zf.write(path, f"scenes/{path.parent.name}/scene.json")
    tmp.replace(out)
    return out


def listing(folder: Path, url_base: str) -> list[dict]:
    out = []
    root = folder / DIR
    for path in sorted(root.glob("*/provenance.json"), key=lambda p: int(p.parent.name)) if root.is_dir() else []:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        n = int(path.parent.name)
        out.append({
            "n": n, "made_at": data.get("made_at"), "preset": data["preset"]["id"], "label": data["preset"]["label"],
            "file": data["output"]["file"], "size": data["output"]["size"], "sha256": data["output"]["sha256"],
            "qa": data.get("qa"), "override_reason": data.get("override_reason"),
            "file_url": f"{url_base}/{n}/file", "provenance_url": f"{url_base}/{n}/provenance.json",
            "archive_url": f"{url_base}/{n}/project.zip",
        })  # fmt: skip
    return out
