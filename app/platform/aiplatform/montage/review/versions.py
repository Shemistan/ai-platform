"""Версии ролика в запуске ревью (ITERATIVE_REVIEW_SPEC §1.2, §8, revision lineage).

`review/versions/V<n>/`: `output.mp4` (жёсткая ссылка на файл проекта — рендер пишет новый файл и
подменяет его, поэтому inode версии не меняется; нет ссылки — копия), манифесты копией,
`version.json` (родитель, раунд, принята ли, sha256 и длительность). V0 — baseline: `save` отказывает,
если каталог версии уже есть — ни одна версия не перезаписывается. `restore` возвращает манифесты и
файл в проект (копией). `diff` — что изменилось между версиями: сцены (по хэшам), длительность, QA.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

from aiplatform.core import media
from aiplatform.core.fsutil import atomic_write
from aiplatform.montage import decisions, schemas

DIR = "review/versions"
MANIFESTS = ("timeline.json", "storyboard.json", "qa_report.json", "plan.json", "edl.json", "timemap.json",
             "subtitles.vtt", "subtitles.srt", "transcript_edit.json", "beats.json", "style_bible.json")  # fmt: skip
OUTPUT = "output.mp4"


class VersionError(ValueError):
    pass


def label(n: int) -> str:
    return f"V{n}"


def number(value: object) -> int | None:
    text = str(value or "")
    return int(text[1:]) if text.startswith("V") and text[1:].isdigit() else None


def folder_of(root: Path, n: int) -> Path:
    return root / DIR / label(n)


def _link_or_copy(src: Path, dst: Path) -> None:
    dst.unlink(missing_ok=True)
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _read(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def save(root: Path, n: int, *, round_n: int, parent: str | None, accepted: bool | None, ffmpeg: str,
         reason: str = "") -> dict:  # fmt: skip
    """Снять версию с текущего состояния проекта; каталог уже есть → VersionError (версии неизменяемы)."""
    target = folder_of(root, n)
    if target.exists():
        raise VersionError(f"Версия {label(n)} уже есть — версии не перезаписываются.")
    output = root / OUTPUT
    if not output.is_file():
        raise VersionError("Ролик ещё не собран — версию снять не с чего.")
    tmp = target.with_name(target.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir(parents=True)
    _link_or_copy(output, tmp / OUTPUT)
    files = [OUTPUT]
    for name in MANIFESTS:
        src = root / name
        if src.is_file():
            shutil.copy2(src, tmp / name)
            files.append(name)
    qa = _read(root / "qa_report.json") or {}
    manifests = {name: _read(root / name) for name in MANIFESTS if name.endswith(".json")}
    record = {
        "n": n,
        "label": label(n),
        "made_at": round(time.time(), 3),
        "round": round_n,
        "parent": parent,
        "accepted": accepted,
        "reason": reason,
        "output": {
            "sha256": decisions.file_sha(output),
            "size": output.stat().st_size,
            "duration": round(media.duration(output, ffmpeg), 3),
        },
        "files": files,
        "qa": {"gates": qa.get("gates"), "counts": qa.get("counts")},
        "project_hash": decisions.sha({k: v for k, v in manifests.items() if v is not None})[:16],
    }
    schemas.check("review_version", record)
    atomic_write(tmp / "version.json", json.dumps(record, ensure_ascii=False, indent=1))
    tmp.rename(target)
    return record


def mark(root: Path, n: int, *, accepted: bool | None, reason: str = "") -> dict | None:
    """Решение по кандидату — единственное, что в версии меняется после снятия."""
    path = folder_of(root, n) / "version.json"
    record = _read(path)
    if record is None:
        return None
    record["accepted"] = accepted
    record["reason"] = reason
    atomic_write(path, json.dumps(record, ensure_ascii=False, indent=1))
    return record


def get(root: Path, n: int) -> dict | None:
    return _read(folder_of(root, n) / "version.json")


def listing(root: Path) -> list[dict]:
    base = root / DIR
    out = []
    for path in sorted(base.glob("V*/version.json"), key=lambda p: number(p.parent.name) or 0) if base.is_dir() else []:
        record = _read(path)
        if record is not None:
            out.append(record)
    return out


def file(root: Path, n: int, name: str) -> Path | None:
    if name not in (OUTPUT, *MANIFESTS, "version.json"):
        return None
    path = folder_of(root, n) / name
    return path if path.is_file() else None


def restore(root: Path, n: int) -> dict | None:
    """Вернуть манифесты и ролик версии в проект (копией — версия остаётся нетронутой)."""
    record = get(root, n)
    if record is None:
        return None
    source = folder_of(root, n)
    for name in MANIFESTS:
        src = source / name
        if src.is_file():
            shutil.copy2(src, root / name)
        else:
            (root / name).unlink(missing_ok=True)
    tmp = root / "output.restore.mp4"
    shutil.copy2(source / OUTPUT, tmp)
    tmp.replace(root / OUTPUT)
    return record


def integrity(root: Path, n: int) -> str | None:
    """Проверка, что файл версии цел (sha256 как при снятии); текст проблемы или None."""
    record = get(root, n)
    if record is None:
        return "версии нет"
    path = folder_of(root, n) / OUTPUT
    if not path.is_file():
        return "файл версии пропал"
    if decisions.file_sha(path) != record["output"]["sha256"]:
        return "файл версии изменён — sha256 не совпадает"
    return None


def diff(root: Path, a: int, b: int) -> dict:
    """Что изменилось от версии a к b: сцены по хэшам, длительность, счётчики QA, ролик."""
    va, vb = get(root, a) or {}, get(root, b) or {}
    sa = {s["id"]: s for s in (_read(folder_of(root, a) / "storyboard.json") or {}).get("scenes") or []}
    sb = {s["id"]: s for s in (_read(folder_of(root, b) / "storyboard.json") or {}).get("scenes") or []}
    changed = sorted(
        sid
        for sid in sa.keys() | sb.keys()
        if (sa.get(sid) or {}).get("hash") != (sb.get(sid) or {}).get("hash")
        or (sa.get(sid) or {}).get("enabled", True) != (sb.get(sid) or {}).get("enabled", True)
        or (sa.get(sid) or {}).get("edit_start_frame") != (sb.get(sid) or {}).get("edit_start_frame")
    )
    frames = []
    for sid in changed:
        for scene in (sa.get(sid), sb.get(sid)):
            if scene:
                frames.append({"from": scene["edit_start_frame"],
                               "to": scene["edit_start_frame"] + scene["duration_frames"]})  # fmt: skip
    ta = _read(folder_of(root, a) / "timeline.json") or {}
    tb = _read(folder_of(root, b) / "timeline.json") or {}
    qa_a, qa_b = (va.get("qa") or {}).get("counts") or {}, (vb.get("qa") or {}).get("counts") or {}
    return {
        "a": label(a),
        "b": label(b),
        "scenes_changed": changed,
        "frames_changed": frames,
        "duration_frames": {"a": ta.get("duration_frames"), "b": tb.get("duration_frames")},
        "qa_delta": {k: qa_b.get(k, 0) - qa_a.get(k, 0) for k in ("error", "warning", "info")},
        "output_changed": (va.get("output") or {}).get("sha256") != (vb.get("output") or {}).get("sha256"),
    }
