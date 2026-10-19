"""Рендер сцен раскадровки (MASTER_SPEC §5.6): HyperFrames → MOV с альфой, кэш по хэшу, проверка, миниатюра.

`scenes/<scene_id>/`: `index.html` (композиция), `<hash>.mov` (ProRes 4444), `thumb.png` (первый
кадр), `scene.json`. Файл с текущим хэшем есть — рендер пропускается: правка одной сцены не
трогает остальные. После рендера — проверка: длительность = кадры/fps (±1 кадр), пиксели с альфой,
кадр 1920×1080; не сошлось — статус failed с причиной. Нет HyperFrames — все сцены blocked с
подсказкой, финал собирается без них (честный статус вместо подделки).
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from aiplatform.core import media, runner
from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import motion, render, templates
from aiplatform.montage.config import MontageConfig

DIR = "scenes"
THUMB_W = 480
TOLERANCE_S = 1.5 / render.FPS


def folder_of(root: Path, scene_id: str) -> Path:
    return root / DIR / scene_id


def mov_of(root: Path, scene: dict) -> Path:
    return folder_of(root, scene["id"]) / f"{scene['hash']}.mov"


def render_argv(config: MontageConfig, out: Path, fps: int) -> list[str]:
    return [*config.hyperframes_cmd, "render", "--composition", "index.html", "--format", "mov", "--fps", str(fps),
            "--output", str(out), "--quiet"]  # fmt: skip


def probe(path: Path, ffmpeg: str) -> dict:
    """Длительность, размер и формат пикселей MOV."""
    argv = [media.ffprobe_for(ffmpeg), "-v", "error", "-select_streams", "v:0", "-show_entries",
            "stream=width,height,pix_fmt:format=duration", "-of", "json", str(path)]  # fmt: skip
    try:
        info = json.loads(subprocess.run(argv, capture_output=True, text=True, check=True, timeout=60).stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    stream = (info.get("streams") or [{}])[0]
    return {"width": stream.get("width"), "height": stream.get("height"), "pix_fmt": stream.get("pix_fmt"),
            "duration": float((info.get("format") or {}).get("duration") or 0)}  # fmt: skip


def validate(path: Path, scene: dict, ffmpeg: str) -> str | None:
    """Причина негодности или None (§5.6: кадры, альфа, размер)."""
    if not path.is_file() or not path.stat().st_size:
        return "файл сцены пуст"
    info = probe(path, ffmpeg)
    if not info:
        return "ffprobe не прочитал сцену"
    expected = scene["duration_frames"] / scene["fps"]["num"]
    if abs(info["duration"] - expected) > TOLERANCE_S:
        return f"длительность {info['duration']:.2f} с вместо {expected:.2f}"
    if (info["width"], info["height"]) != (templates.W, templates.H):
        return f"кадр {info['width']}×{info['height']} вместо {templates.W}×{templates.H}"
    if not str(info.get("pix_fmt") or "").startswith("yuva") and "a" not in str(info.get("pix_fmt") or ""):
        return f"нет альфа-канала ({info.get('pix_fmt')})"
    return None


def thumbnail(mov: Path, out: Path, ffmpeg: str) -> bool:
    argv = [ffmpeg, "-y", "-loglevel", "error", "-i", str(mov), "-frames:v", "1", "-vf",
            f"scale={THUMB_W}:-2,format=rgba", str(out)]  # fmt: skip
    try:
        return subprocess.run(argv, capture_output=True, timeout=60).returncode == 0 and out.is_file()
    except (OSError, subprocess.SubprocessError):
        return False


def render_all(
    config: MontageConfig,
    root: Path,
    scenes: list[dict],
    style: dict,
    cancel: threading.Event,
    *,
    note: Callable[[str], None],
    progress: Callable[[str, float], None],
) -> list[dict]:
    """Все включённые сцены: кэш по хэшу, иначе HyperFrames; статусы rendered/failed/blocked."""
    out = [dict(s) for s in scenes]
    todo = [s for s in out if s.get("enabled", True) and s["status"] != "disabled"]
    if not config.hyperframes_available():
        for scene in todo:
            scene.update(status="blocked", error=motion.INSTALL_HINT)
        note(f"Сцены не отрисованы: {motion.INSTALL_HINT}")
        return out
    for n, scene in enumerate(todo, 1):
        if cancel.is_set():
            raise runner.Canceled()
        progress(f"сцена {n} из {len(todo)}: {scene['template_id']}", 100 * (n - 1) / max(1, len(todo)))
        folder = folder_of(root, scene["id"])
        folder.mkdir(parents=True, exist_ok=True)
        mov = mov_of(root, scene)
        duration_s = scene["duration_frames"] / scene["fps"]["num"]
        document = templates.composition(scene["template_id"], scene["props"], style, duration_s, title=scene["intent"])
        atomic_write(folder / "index.html", document)
        if not (mov.is_file() and mov.stat().st_size):
            for old in folder.glob("*.mov"):  # прошлые версии сцены — не нужны
                old.unlink(missing_ok=True)
            tmp = folder / f"{scene['hash']}.tmp.mov"
            try:
                argv = render_argv(config, tmp, scene["fps"]["num"])
                runner.run(argv, lambda line: note(line) if line.strip() else None, cancel, cwd=folder,
                           missing_hint=motion.INSTALL_HINT, kind="render")  # fmt: skip
                tmp.replace(mov)
            except runner.RunFailed as exc:
                tmp.unlink(missing_ok=True)
                scene.update(status="failed", error=f"рендер не удался: {exc}"[:300])
                note(f"Сцена {scene['id']} не отрисовалась: {exc}")
                _save(folder, scene)
                continue
        reason = validate(mov, scene, config.ffmpeg)
        if reason:
            scene.update(status="failed", error=reason)
            note(f"Сцена {scene['id']} не прошла проверку: {reason}")
        else:
            scene.update(status="rendered", error=None)
            thumbnail(mov, folder / "thumb.png", config.ffmpeg)
        _save(folder, scene)
    progress("сцены готовы", 100)
    return out


def _save(folder: Path, scene: dict) -> None:
    atomic_write(folder / "scene.json", json.dumps(scene, ensure_ascii=False, indent=2))


def overlays(root: Path, scenes: list[dict]) -> list[render.Overlay]:
    """Наложения для финального прохода: отрисованные и включённые сцены по кадрам результата."""
    out = []
    for scene in sorted(scenes, key=lambda s: s["edit_start_frame"]):
        if scene["status"] != "rendered" or not scene.get("enabled", True):
            continue
        mov = mov_of(root, scene)
        if mov.is_file():
            fps = scene["fps"]["num"]
            out.append(render.Overlay(mov, scene["edit_start_frame"] / fps, scene["duration_frames"] / fps))
    return out
