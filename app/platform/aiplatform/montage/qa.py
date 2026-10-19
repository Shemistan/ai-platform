"""QA готового ролика (MASTER_SPEC §5.8): механические проверки, семантика, qa_report.json с воротами.

Механика — один проход ffmpeg по output.mp4 (blackdetect + silencedetect + astats) и ffprobe:
длительность против таймлайна (±1 кадр, плюс документированный хвост loudnorm), дорожки, размер и
частота кадров, чёрные кадры, длинные паузы, клиппинг, сцены (файл есть, в границах, статус),
субтитры длиннее max_chars, шрифт для субтитров в кадре. Семантика — повторная расшифровка и LLM
(круги проверки в projects.py): без LLM — `human_review`. Ремонт: круги проверки ≤ MAX_ROUNDS —
`attempts`; дальше `escalated`. Экспорт возможен, если механические ворота `pass`, иначе — только с
записанной причиной (projects.export).
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path

from aiplatform.core import media, runner
from aiplatform.montage import render, schemas, transcript
from aiplatform.montage.config import MontageConfig

FILE = "qa_report.json"
LOUDNORM_TAIL_S = 0.25  # loudnorm может дописать до четверти секунды тишины в хвост (docs/TIMEBASE.md)
BLACK_MIN_S = 0.5
CLIP_PEAK_DB = -0.1
_BLACK = re.compile(r"black_start:(\d+(?:\.\d+)?)\s+black_end:(\d+(?:\.\d+)?)")
_PEAK = re.compile(r"Peak level dB:\s*(-?\d+(?:\.\d+)?|-inf)")


def _item(n: int, kind: str, check: str, severity: str, message: str, *, evidence: dict | None = None,
          fix: str = "", attempts: int = 0, status: str = "open") -> dict:  # fmt: skip
    return {"id": f"qa_{n:03d}", "kind": kind, "check": check, "severity": severity, "message": message,
            "evidence": evidence or {}, "proposed_fix": fix, "attempts": attempts, "status": status}  # fmt: skip


def analyze_argv(ffmpeg: str, path: Path, pause_s: float) -> list[str]:
    return [ffmpeg, "-hide_banner", "-nostats", "-i", str(path), "-vf", f"blackdetect=d={BLACK_MIN_S}:pix_th=0.10",
            "-af", f"silencedetect=noise={transcript.SILENCE_DB}dB:d={pause_s},astats=metadata=1:reset=0",
            "-f", "null", "-"]  # fmt: skip


def parse_analysis(lines: list[str], duration: float) -> dict:
    """Чёрные кадры, тишина и пик из stderr ffmpeg."""
    black = [(float(a), float(b)) for a, b in (m.groups() for m in map(_BLACK.search, lines) if m)]
    peaks = [float(m[1]) for m in map(_PEAK.search, lines) if m and m[1] != "-inf"]
    return {
        "black": black,
        "silence": transcript.parse_silences(lines, duration),
        "peak_db": max(peaks) if peaks else None,
    }


def probe_streams(path: Path, ffmpeg: str) -> dict:
    argv = [media.ffprobe_for(ffmpeg), "-v", "error", "-show_entries",
            "stream=codec_type,width,height,r_frame_rate,sample_rate:format=duration", "-of", "json",
            str(path)]  # fmt: skip
    try:
        info = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    video = next((s for s in info.get("streams") or [] if s.get("codec_type") == "video"), {})
    audio = next((s for s in info.get("streams") or [] if s.get("codec_type") == "audio"), {})
    num, _, den = str(video.get("r_frame_rate") or "0/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except ValueError:
        fps = 0.0
    return {
        "duration": float((info.get("format") or {}).get("duration") or 0),
        "video": bool(video), "audio": bool(audio), "width": video.get("width"), "height": video.get("height"),
        "fps": round(fps, 3), "audio_rate": int(audio.get("sample_rate") or 0),
    }  # fmt: skip


def mechanical(config: MontageConfig, folder: Path, project: dict, timeline: dict, scenes: list[dict],
               captions: list[dict], style: dict | None, cancel: threading.Event) -> list[dict]:  # fmt: skip
    """Список QAItem по output.mp4 и манифестам (§5.8, механика)."""
    out: list[dict] = []
    n = 0

    def add(*args, **kwargs) -> None:
        nonlocal n
        n += 1
        out.append(_item(n, "mechanical", *args, **kwargs))

    path = folder / "output.mp4"
    info = probe_streams(path, config.ffmpeg)
    fps = timeline["fps"]
    expected = timeline["duration_frames"] / fps
    if not info:
        add("streams", "error", "ffprobe не читает output.mp4", fix="пересобрать ролик")
        return out
    if not info["video"]:
        add("streams", "error", "в результате нет видеодорожки", fix="пересобрать ролик")
    if not info["audio"]:
        add("streams", "error", "в результате нет звука", fix="проверить запись «голова» и пересобрать")
    drift = info["duration"] - expected
    if drift < -1.5 / fps or drift > 1.5 / fps + LOUDNORM_TAIL_S:
        add("duration", "error", f"длительность {info['duration']:.2f} с, по таймлайну {expected:.2f} с",
            evidence={"value": f"{drift:+.3f}"}, fix="пересобрать: карта времени и сегменты разошлись")  # fmt: skip
    if (info["width"], info["height"]) != (render.W, render.H):
        add("resolution", "error", f"кадр {info['width']}×{info['height']} вместо {render.W}×{render.H}")
    if abs(info["fps"] - fps) > 0.01:
        add("fps", "error", f"частота кадров {info['fps']} вместо {fps}")
    if info["audio"] and info["audio_rate"] != 48000:
        add("audio_rate", "warning", f"звук {info['audio_rate']} Гц вместо 48000")
    lines: list[str] = []
    pause = project["options"]["max_pause"] + 0.3
    runner.run(analyze_argv(config.ffmpeg, path, pause), lines.append, cancel, kind="render")
    found = parse_analysis(lines, info["duration"])
    for a, b in found["black"]:
        add("black_frames", "error", f"чёрные кадры {b - a:.1f} с на {a:.1f} с", evidence={"t_start": a, "t_end": b},
            fix="проверить сегмент на этом месте: пустой кусок записи или экран без сигнала")  # fmt: skip
    for a, b in found["silence"]:
        if b - a >= pause and a > 0.5 and b < info["duration"] - 0.3:
            add("silence", "warning", f"пауза {b - a:.1f} с на {a:.1f} с", evidence={"t_start": a, "t_end": b},
                fix="вырезать паузу в плане (клик по словам вокруг) или уменьшить «самую длинную паузу»")  # fmt: skip
    if found["peak_db"] is not None and found["peak_db"] > CLIP_PEAK_DB:
        add("clipping", "warning", f"пик {found['peak_db']:.1f} дБ — возможен клиппинг",
            evidence={"value": f"{found['peak_db']:.2f}"}, fix="снизить громкость записи или TP loudnorm")  # fmt: skip
    total = timeline["duration_frames"]
    for scene in scenes:
        if not scene.get("enabled", True):
            continue
        end = scene["edit_start_frame"] + scene["duration_frames"]
        if end > total:
            add("scene_bounds", "error", f"{scene['id']} выходит за конец ролика",
                evidence={"scene_id": scene["id"], "frame": end}, fix="сдвинуть или укоротить сцену")  # fmt: skip
        if scene["status"] == "blocked":
            add("scene_blocked", "warning", f"{scene['id']} не отрисована: {scene.get('error') or 'заблокирована'}",
                evidence={"scene_id": scene["id"]}, fix="поставить HyperFrames или выключить сцену")  # fmt: skip
        elif scene["status"] == "failed":
            add("scene_failed", "error", f"{scene['id']} не прошла рендер: {scene.get('error') or ''}",
                evidence={"scene_id": scene["id"]}, fix="переделать сцену по замечанию или выключить")  # fmt: skip
        elif scene["status"] == "rendered" and not (folder / "scenes" / scene["id"] / f"{scene['hash']}.mov").is_file():
            add("asset_missing", "error", f"файл сцены {scene['id']} пропал", evidence={"scene_id": scene["id"]},
                fix="пересобрать: сцена отрисуется заново")  # fmt: skip
    limit = (style or {}).get("captions", {}).get("max_chars", 42)
    for cap in captions:
        if len(cap["text"]) > limit:
            add("caption_overflow", "warning", f"субтитр длиннее {limit} знаков: «{cap['text'][:40]}…»",
                evidence={"t_start": cap["s"], "t_end": cap["e"]}, fix="короче куски субтитров")  # fmt: skip
    if project["options"]["subtitles"] and not config.font:
        add("font_missing", "warning", "нет шрифта для субтитров в кадре (AIP_FONT)", fix="указать AIP_FONT")
    return out


def semantic(project: dict, rounds: list[dict], *, llm_available: bool, max_rounds: int) -> tuple[list[dict], str]:
    """(QAItem семантики, режим ворот): найденное и вырезанное проверкой — fixed; последний круг с находками —
    escalated; без проверки или LLM — human_review."""
    if not project["options"]["check"] or not llm_available:
        return [], "human_review"
    out: list[dict] = []
    n = 100
    for rnd in rounds:
        for issue in rnd["issues"]:
            n += 1
            last = rnd["round"] > max_rounds
            out.append(_item(n, "semantic", issue.get("kind", "check"), "warning" if last else "info",
                             issue.get("reason") or "найдено повторной расшифровкой",
                             evidence={"value": f"слова {issue['from']}–{issue['to']}"},
                             fix="вырезано и пересобрано" if not last else "осталось после двух кругов — посмотри сам",
                             attempts=rnd["round"], status="escalated" if last else "fixed"))  # fmt: skip
    mode = "fail" if any(i["status"] == "escalated" for i in out) else "pass"
    return out, mode


def report(mechanical_items: list[dict], semantic_items: list[dict], semantic_mode: str, attempts: int) -> dict:
    items = [*mechanical_items, *semantic_items]
    schemas.check("qa_item", items, many=True)
    open_errors = [i for i in mechanical_items if i["severity"] == "error" and i["status"] == "open"]
    return {
        "schema_version": schemas.SCHEMA_VERSION,
        "made_at": round(time.time(), 3),
        "items": items,
        "gates": {"mechanical": "fail" if open_errors else "pass", "semantic": semantic_mode},
        "repair_attempts": attempts,
        "counts": {
            "error": sum(1 for i in items if i["severity"] == "error"),
            "warning": sum(1 for i in items if i["severity"] == "warning"),
            "info": sum(1 for i in items if i["severity"] == "info"),
        },
    }


def load(folder: Path) -> dict | None:
    try:
        return json.loads((folder / FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
