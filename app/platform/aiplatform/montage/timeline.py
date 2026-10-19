"""Таймлайн-манифест (MASTER_SPEC §5.7): дорожки и клипы в кадрах результата — из него собирается финал.

Дорожки: presenter (сегменты «головы»), screen (те же сегменты с экраном, если он есть), overlays
(сцены раскадровки или быстрые вставки — с путём к MOV), captions (куски субтитров), dialogue (звук
сегментов). Клипы — в целых кадрах по TimeMap; финальный проход берёт наложения отсюда
(`overlays`), а не из раскадровки напрямую: превью и финал — от одного манифеста. Ручной
drag-and-drop не входит (ADR 0011): клипы правятся через сцены (кадры, enabled).
"""

from __future__ import annotations

from pathlib import Path

from aiplatform.montage import render, schemas, timemap

TRACKS = (("presenter", "video"), ("screen", "video"), ("overlays", "overlay"), ("captions", "captions"),
          ("dialogue", "audio"))  # fmt: skip


def build(tm: timemap.TimeMap, scenes: list[dict], captions: list[dict], *, has_screen: bool,
          inserts: list[dict] | None = None) -> dict:  # fmt: skip
    """Манифест по карте времени, сценам (enabled + rendered → клип с path) и кускам субтитров.

    inserts — быстрые вставки motion.run: {"at", "duration", "path"} в секундах результата."""
    presenter, screen, dialogue = [], [], []
    for n, seg in enumerate(tm.segments, 1):
        clip = {
            "id": f"seg_{n:03d}", "source_id": seg.source_id, "source_in": round(seg.source_pts_start, 3),
            "source_out": round(seg.source_pts_end, 3), "edit_in_frame": seg.edit_frame_start,
            "edit_out_frame": seg.edit_frame_end, "layout": seg.layout, "enabled": True,
        }  # fmt: skip
        presenter.append(clip)
        if has_screen and seg.layout in ("screen", "split"):
            screen.append({**clip, "id": f"scr_{n:03d}", "source_id": "screen"})
        dialogue.append({**{k: v for k, v in clip.items() if k != "layout"}, "id": f"aud_{n:03d}", "gain_db": 0.0})
    overlays = []
    for scene in sorted(scenes, key=lambda s: s["edit_start_frame"]):
        if not scene.get("enabled", True):
            continue
        overlays.append({
            "id": scene["id"], "scene_id": scene["id"], "edit_in_frame": scene["edit_start_frame"],
            "edit_out_frame": scene["edit_start_frame"] + scene["duration_frames"],
            "enabled": scene["status"] == "rendered", "opacity": 1.0,
            "path": f"scenes/{scene['id']}/{scene['hash']}.mov" if scene["status"] == "rendered" else "",
            "status": scene["status"],
        })  # fmt: skip
    for n, item in enumerate(inserts or [], 1):
        overlays.append({
            "id": f"insert_{n:02d}", "edit_in_frame": tm.frame_of(item["at"]),
            "edit_out_frame": tm.frame_of(item["at"] + item["duration"]), "enabled": True, "opacity": 1.0,
            "path": item["path"], "status": "rendered",
        })  # fmt: skip
    caps = []
    for n, c in enumerate(captions, 1):
        start = tm.frame_of(c["s"])
        end = max(start + 1, tm.frame_of(c["e"]))
        caps.append({"id": f"cap_{n:03d}", "edit_in_frame": start, "edit_out_frame": end, "text": c["text"],
                     "enabled": True})  # fmt: skip
    by_id = {"presenter": presenter, "screen": screen, "overlays": overlays, "captions": caps, "dialogue": dialogue}
    data = {
        "schema_version": schemas.SCHEMA_VERSION,
        "fps": tm.fps,
        "duration_frames": tm.duration_frames,
        "tracks": [
            {"id": tid, "kind": kind, "clips": by_id[tid]} for tid, kind in TRACKS if tid != "screen" or has_screen
        ],
    }
    schemas.check("timeline", data)
    return data


def check(data: dict) -> list[str]:
    """Нарушения: клип за концом результата, пересечения в одной дорожке (кроме overlays и captions)."""
    out: list[str] = []
    total = data["duration_frames"]
    for track in data["tracks"]:
        last = 0
        for clip in sorted(track["clips"], key=lambda c: c["edit_in_frame"]):
            if clip["edit_out_frame"] <= clip["edit_in_frame"]:
                out.append(f"{track['id']}/{clip['id']}: пустой клип")
            if clip["edit_out_frame"] > total:
                out.append(f"{track['id']}/{clip['id']}: за концом ролика ({clip['edit_out_frame']} > {total})")
            if track["id"] in ("presenter", "dialogue") and clip["edit_in_frame"] < last:
                out.append(f"{track['id']}/{clip['id']}: пересекается с предыдущим")
            last = max(last, clip["edit_out_frame"])
    return out


def overlays(data: dict, root: Path) -> list[render.Overlay]:
    """Наложения финального прохода: включённые клипы дорожки overlays с существующим файлом."""
    fps = data["fps"]
    out = []
    for track in data["tracks"]:
        if track["id"] != "overlays":
            continue
        for clip in track["clips"]:
            path = root / clip.get("path", "") if clip.get("path") else None
            if not clip["enabled"] or path is None or not path.is_file():
                continue
            frames = clip["edit_out_frame"] - clip["edit_in_frame"]
            out.append(render.Overlay(path, clip["edit_in_frame"] / fps, frames / fps))
    return out


def summary(data: dict) -> dict:
    return {t["id"]: len(t["clips"]) for t in data["tracks"]} | {"duration_frames": data["duration_frames"]}
