"""Review bundle (ITERATIVE_REVIEW_SPEC §3–4): что видит каждая роль — одно и то же, из настоящего ролика.

Один проход ffmpeg по `output.mp4` версии: прокси 640p (`proxy.mp4`), кадры начала/середины/конца
каждой сцены и равномерно раз в UNIFORM_S (`frames/f<кадр>.jpg`), `signalstats` (яркость YAVG и
разница кадров YDIF, 2 к/с — вспышки и тёмные места), `ebur128` (громкость, true peak), `silencedetect`.
Манифест — `bundle.json`; тексты для LLM-ролей (`texts`): расшифровка результата с кадрами, биты,
утверждения, сцены, стиль, таймлайн, QA, история и замечания прошлого раунда. Роли не считают, что
«видели видео», если у них только текст: evidence_type у текстовых ролей — transcript/heuristic.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import media
from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import knowledge, narrative, plan, qa, render, transcript
from aiplatform.montage import style as style_module
from aiplatform.montage.config import MontageConfig

DIR = "bundle"
PROXY_W = 640
FRAME_W = 480
UNIFORM_S = 10.0
STATS_FPS = 2
MAX_FRAMES = 400
_STATS_T = re.compile(r"pts_time:(\d+(?:\.\d+)?)")
_STATS_KV = re.compile(r"lavfi\.signalstats\.(YAVG|YDIF)=(-?\d+(?:\.\d+)?)")
_LUFS = re.compile(r"^\s*I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", re.M)
_LRA = re.compile(r"^\s*LRA:\s*(-?\d+(?:\.\d+)?)\s*LU", re.M)
_PEAK = re.compile(r"^\s*Peak:\s*(-?\d+(?:\.\d+)?)\s*dBFS", re.M)


def folder_of(round_dir: Path) -> Path:
    return round_dir / DIR


def _read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def frame_points(scenes: list[dict], duration_frames: int, fps: int) -> list[dict]:
    """Какие кадры вынуть: начало/середина/конец включённых сцен + равномерно; не больше MAX_FRAMES."""
    points: dict[int, dict] = {}
    for scene in scenes:
        if not scene.get("enabled", True):
            continue
        start, dur = scene["edit_start_frame"], scene["duration_frames"]
        for kind, frame in (("scene_start", start), ("scene_mid", start + dur // 2), ("scene_end", start + dur - 1)):
            frame = max(0, min(duration_frames - 1, frame))
            points.setdefault(frame, {"frame": frame, "kind": kind, "scene_id": scene["id"]})
    step = max(1, round(UNIFORM_S * fps))
    for frame in range(0, max(duration_frames, 1), step):
        points.setdefault(frame, {"frame": frame, "kind": "uniform", "scene_id": None})
    out = sorted(points.values(), key=lambda p: p["frame"])
    if len(out) > MAX_FRAMES:  # очень длинный ролик: сцены важнее равномерных кадров
        scene_points = [p for p in out if p["kind"] != "uniform"][:MAX_FRAMES]
        uniform = [p for p in out if p["kind"] == "uniform"]
        keep = max(0, MAX_FRAMES - len(scene_points))
        out = sorted([*scene_points, *uniform[:: max(1, len(uniform) // max(keep, 1))][:keep]],
                     key=lambda p: p["frame"])  # fmt: skip
    for p in out:
        p["t"] = round(p["frame"] / fps, 3)
        p["file"] = f"frames/f{p['frame']:06d}.jpg"
    return out


def argv(config: MontageConfig, video: Path, out: Path, points: list[dict], *, pause_s: float) -> list[str]:
    """Один ffmpeg: прокси, кадры по списку, signalstats в файл, ebur128 + silencedetect в stderr."""
    select = "+".join(f"eq(n\\,{p['frame']})" for p in points) or "0"
    stats = out / "stats.txt"
    audio = f"ebur128=peak=true,silencedetect=noise={transcript.SILENCE_DB}dB:d={pause_s}"
    return [
        config.ffmpeg, "-y", "-hide_banner", "-nostats", "-loglevel", "info", "-i", str(video),
        "-map", "0:v", "-map", "0:a?", "-vf", f"scale={PROXY_W}:-2", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "28", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        str(out / "proxy.mp4"),
        "-map", "0:v", "-vf", f"select='{select}',scale={FRAME_W}:-2", "-vsync", "0", "-q:v", "4",
        str(out / "frames" / "x%06d.jpg"),
        "-map", "0:v", "-vf", f"fps={STATS_FPS},scale=160:-2,signalstats,metadata=print:file={stats}", "-f", "null", "-",
        "-map", "0:a?", "-af", audio, "-f", "null", "-",
    ]  # fmt: skip


def parse_stats(text: str) -> list[dict]:
    """metadata=print → [{t, yavg, ydif}] по кадрам статистики."""
    out: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        found = _STATS_T.search(line)
        if found:
            current = {"t": round(float(found[1]), 3), "yavg": None, "ydif": None}
            out.append(current)
            continue
        kv = _STATS_KV.search(line)
        if kv and current is not None:
            current[kv[1].lower()] = float(kv[2])
    return out


def parse_audio(lines: list[str], duration: float, pause_s: float) -> dict:
    text = "\n".join(lines)
    lufs, lra, peak = _LUFS.search(text), _LRA.search(text), _PEAK.search(text)
    silences = [(a, b) for a, b in transcript.parse_silences(lines, duration) if b - a >= pause_s]
    return {
        "integrated_lufs": float(lufs[1]) if lufs else None,
        "lra_lu": float(lra[1]) if lra else None,
        "true_peak_dbfs": float(peak[1]) if peak else None,
        "silences": [[round(a, 2), round(b, 2)] for a, b in silences],
    }


def build(config: MontageConfig, version_dir: Path, out: Path, cancel: threading.Event, *,
          run_ffmpeg: Callable[[list[str], threading.Event, float, Callable[[float], None], list[str]], list[str]],
          pause_s: float, progress: Callable[[str, float], None] | None = None) -> dict:  # fmt: skip
    """Собрать бандл версии в out; уже собран (bundle.json есть) — вернуть как есть (resume без пересчёта)."""
    manifest_path = out / "bundle.json"
    if manifest_path.is_file():
        return _read(manifest_path)
    video = version_dir / "output.mp4"
    timeline = _read(version_dir / "timeline.json")
    board = _read(version_dir / "storyboard.json")
    fps = int(timeline.get("fps") or render.FPS)
    duration_frames = int(timeline.get("duration_frames") or round(media.duration(video, config.ffmpeg) * fps))
    points = frame_points(board.get("scenes") or [], duration_frames, fps)
    out.mkdir(parents=True, exist_ok=True)
    (out / "frames").mkdir(exist_ok=True)
    duration = media.duration(video, config.ffmpeg)
    lines: list[str] = []
    say = progress or (lambda _d, _p: None)
    run_ffmpeg(argv(config, video, out, points, pause_s=pause_s), cancel, duration,
               lambda pct: say("прокси, кадры, замеры", pct * 0.95), lines)  # fmt: skip
    # Кадры select идут по порядку — переименовать x000001 → f<кадр>.
    produced = sorted((out / "frames").glob("x*.jpg"))
    for point, path in zip(points, produced, strict=False):
        path.replace(out / point["file"])
    for extra in produced[len(points) :]:
        extra.unlink(missing_ok=True)
    frames = [p for p in points if (out / p["file"]).is_file()]
    try:
        stats = parse_stats((out / "stats.txt").read_text(encoding="utf-8"))
    except OSError:
        stats = []
    manifest = {
        "version": version_dir.name,
        "made_at": round(__import__("time").time(), 3),
        "fps": fps,
        "duration_frames": duration_frames,
        "duration_s": round(duration, 3),
        "proxy": "proxy.mp4" if (out / "proxy.mp4").is_file() else None,
        "frames": frames,
        "video_stats": stats,
        "audio": parse_audio(lines, duration, pause_s),
        "has_audio": any("Integrated loudness" in ln or ln.strip().startswith("I:") for ln in lines),
    }
    atomic_write(manifest_path, json.dumps(manifest, ensure_ascii=False))
    say("бандл готов", 100)
    return manifest


def load(out: Path) -> dict:
    return _read(out / "bundle.json")


def texts(version_dir: Path, folder: Path, *, previous_issues: list[dict] | None = None, fps: int = render.FPS) -> dict:
    """Тексты для LLM-ролей и судьи: из манифестов версии (не из текущего проекта — версия immutable)."""
    words = (_read(version_dir / "transcript_edit.json") or {}).get("words") or []
    beats = (_read(version_dir / "beats.json") or {}).get("beats") or []
    claims = (_read(folder / "knowledge.json") or {}).get("claims") or []
    scenes = (_read(version_dir / "storyboard.json") or {}).get("scenes") or []
    style = _read(version_dir / "style_bible.json") or style_module.DEFAULT
    report = _read(version_dir / "qa_report.json")
    timeline = _read(version_dir / "timeline.json")
    captions = next((t["clips"] for t in timeline.get("tracks") or [] if t["id"] == "captions"), [])
    packed = plan.pack([{k: w[k] for k in ("w", "s", "e")} for w in words]) if words else "(расшифровки нет)"
    scene_lines = []
    for s in scenes:
        if not s.get("enabled", True):
            continue
        a, b = s["edit_start_frame"], s["edit_start_frame"] + s["duration_frames"]
        scene_lines.append(f"- {s['id']} [{a / fps:.1f}–{b / fps:.1f} с, кадры {a}–{b}] {s['template_id']} "
                           f"({s['status']}, бит {s['beat_id']}, утв. {', '.join(s.get('grounding_claim_ids') or []) or '—'}): "
                           f"{s['intent']}")  # fmt: skip
    qa_lines = [f"- {i['check']} ({i['severity']}): {i['message']}" for i in report.get("items") or []][:40]
    prev = [f"- {i['issue_id']} [{i['status']}] {i['category']}: {i['observation']}" for i in previous_issues or []][
        :40
    ]
    longest = max(((c["edit_out_frame"] - c["edit_in_frame"]) / fps for c in captions), default=0.0)
    return {
        "transcript": packed,
        "beats": narrative.pack_beats(beats) if beats else "- (битов нет)",
        "claims": knowledge.pack_claims(claims),
        "scenes": "\n".join(scene_lines) or "- (сцен нет)",
        "style": style_module.describe(style),
        "qa": "\n".join(qa_lines) or "- (замечаний QA нет)",
        "captions": f"кусков субтитров {len(captions)}, самый долгий {longest:.1f} с",
        "previous": "\n".join(prev) or "- (первый раунд)",
        "duration_s": round((timeline.get("duration_frames") or 0) / fps, 1),
        "scenes_count": len([s for s in scenes if s.get("enabled", True)]),
    }


def qa_items(version_dir: Path) -> list[dict]:
    return (qa.load(version_dir) or {}).get("items") or []
