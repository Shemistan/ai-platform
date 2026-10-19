"""Реестр возможностей «Монтажа» (MASTER_SPEC §3): что реально есть на машине и куда идёт задача.

Адаптеры платформы — CLI-подпроцессы (ADR 0005/0006): воркер расшифровки, `claude -p`, HyperFrames,
ffmpeg с фильтрами, Blender, pdftotext. `registry` — дешёвая проверка (файл есть, модель
установлена); `probe` запускает настоящую команду (`--version`/`-h`) с таймаутом и записывает
версию. `route(task)` — детерминированный выбор движка или `blocked` с причиной: без HyperFrames
сцены не рисуются, без pdftotext PDF не читается, 3D — нет Blender. Ничего не скачивается.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

from aiplatform.core import media
from aiplatform.montage import asr, motion
from aiplatform.montage.config import MontageConfig
from aiplatform.studio import llm

PROBE_TIMEOUT_S = 20
FILTERS = ("subtitles", "drawtext", "loudnorm", "silencedetect", "blackdetect", "astats")
PDFTOTEXT_HINT = "brew install poppler — тогда PDF-материалы станут читаться."
BLENDER_HINT = "3D-сцен в платформе нет; Blender на машине не найден. Сцены идут в 2D (HyperFrames)."
TASKS = {
    "transcribe": "расшифровка",
    "plan": "план вырезок (LLM)",
    "story": "знания, биты, стиль, раскадровка (LLM)",
    "scene_render": "рендер моушн-сцен",
    "scene_3d": "3D-сцены",
    "document_pdf": "чтение PDF",
    "document_docx": "чтение DOCX",
    "final": "финальный проход ffmpeg",
}


def _which(cmd: tuple[str, ...]) -> str | None:
    if not cmd:
        return None
    head = cmd[0]
    return head if Path(head).is_file() else shutil.which(head)


def _entry(cid: str, kind: str, label: str, ok: bool, *, detail: str = "", hint: str = "", disabled: bool = False,
           license: str = "", deterministic: bool = True, version: str | None = None) -> dict:  # fmt: skip
    status = "disabled" if disabled else ("available" if ok else "unavailable")
    return {
        "id": cid, "kind": kind, "label": label, "status": status, "version": version, "detail": detail,
        "probed_at": None, "hint": hint if not ok or disabled else "", "license": license,
        "deterministic": deterministic,
    }  # fmt: skip


def registry(config: MontageConfig) -> list[dict]:
    """Состояние без запуска команд: установлено ли, не выключено ли."""
    out: list[dict] = []
    disabled = config.disabled_models()
    for model in asr.CATALOG:
        installed = asr.installed(model, config)
        out.append(_entry(
            f"asr:{model.id}", "asr", model.label, installed, disabled=model.id in disabled and installed,
            detail="воркер mlx-audio" if config.asr_available() else "нет окружения mlx-audio",
            hint=f"make models M={model.id}" if not installed else "включи во вкладке «Настройки»",
            license=getattr(model, "license", ""), deterministic=False,
        ))  # fmt: skip
    llm_ok = config.llm_available()
    out.append(_entry("llm", "llm", "LLM (" + " ".join(config.llm_cmd[-2:]) + ")", llm_ok,
                      detail="фейк" if config.fake else " ".join(config.llm_cmd),
                      hint=llm.missing_cli(config.llm_cmd[0]) if not llm_ok else "", deterministic=False))  # fmt: skip
    hf_ok = config.hyperframes_available()
    out.append(_entry("hyperframes", "render", "HyperFrames", hf_ok, detail=" ".join(config.hyperframes_cmd),
                      hint=motion.INSTALL_HINT, license="Apache-2.0"))  # fmt: skip
    ff_ok = media.ffmpeg_available(config.ffmpeg)
    missing = [f for f in FILTERS if ff_ok and not media.has_filter(config.ffmpeg, f)]
    out.append(_entry("ffmpeg", "media", "ffmpeg", ff_ok and not missing, detail=config.ffmpeg,
                      hint=("нет фильтров: " + ", ".join(missing) + " — brew install ffmpeg-full") if missing
                      else "brew install ffmpeg-full"))  # fmt: skip
    out.append(_entry("blender", "3d", "Blender", shutil.which("blender") is not None, detail="blender",
                      hint=BLENDER_HINT, license="GPL-2.0+"))  # fmt: skip
    out.append(_entry("pdftotext", "document", "pdftotext (poppler)", shutil.which("pdftotext") is not None,
                      detail="pdftotext", hint=PDFTOTEXT_HINT, license="GPL-2.0+"))  # fmt: skip
    out.append(_entry("docx", "document", "DOCX (stdlib zip+xml)", True, detail="встроено"))
    return out


def _version(argv: list[str], cwd: Path | None = None) -> tuple[bool, str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT_S, cwd=cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)[:200]
    text = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode == 0, (text[0] if text else "")[:120]


def probe(config: MontageConfig) -> list[dict]:
    """registry + настоящий запуск каждой команды; статус по факту ответа."""
    out = registry(config)
    now = round(time.time(), 1)
    for item in out:
        if item["status"] != "available":
            item["probed_at"] = now
            continue
        ok, text = True, ""
        if item["id"] == "llm" and not config.fake:
            ok, text = _version([config.llm_cmd[0], "--version"])
        elif item["id"] == "hyperframes" and not config.fake:
            ok, text = _version([*config.hyperframes_cmd, "--version"])
        elif item["id"] == "ffmpeg":
            ok, text = _version([config.ffmpeg, "-version"])
        elif item["id"] == "blender":
            ok, text = _version(["blender", "--version"])
        elif item["id"] == "pdftotext":
            ok, text = _version(["pdftotext", "-v"])
        elif item["id"].startswith("asr:") and not config.fake:
            ok, text = _version([str(config.asr_python), "--version"])
        item["probed_at"] = now
        item["version"] = text or None
        if not ok:
            item["status"] = "unavailable"
            item["detail"] = f"команда не ответила: {text}" if text else "команда не ответила"
    return out


def route(config: MontageConfig, task: str, registry_items: list[dict] | None = None) -> tuple[str, str]:
    """(движок, причина): движок — id записи реестра или «blocked»/«unavailable»; причина — для человека."""
    items = {i["id"]: i for i in (registry_items or registry(config))}

    def ok(cid: str) -> bool:
        return items.get(cid, {}).get("status") == "available"

    if task == "transcribe":
        for item in items.values():
            if item["kind"] == "asr" and item["status"] == "available":
                return item["id"], ""
        return "blocked", asr_hint(items)
    if task in ("plan", "story"):
        return ("llm", "") if ok("llm") else ("blocked", items["llm"]["hint"])
    if task == "scene_render":
        return ("hyperframes", "") if ok("hyperframes") else ("blocked", items["hyperframes"]["hint"])
    if task == "scene_3d":
        return "blocked", BLENDER_HINT
    if task == "document_pdf":
        return ("pdftotext", "") if ok("pdftotext") else ("unavailable", PDFTOTEXT_HINT)
    if task == "document_docx":
        return "docx", ""
    if task == "final":
        return ("ffmpeg", "") if ok("ffmpeg") else ("blocked", items["ffmpeg"]["hint"])
    return "blocked", f"неизвестная задача {task}"


def asr_hint(items: dict) -> str:
    hints = [i["hint"] for i in items.values() if i["kind"] == "asr" and i["hint"]]
    return hints[0] if hints else "нет модели расшифровки"


def public(config: MontageConfig, *, probed: bool = False) -> dict:
    items = probe(config) if probed else registry(config)
    return {
        "items": items,
        "routes": {task: dict(zip(("engine", "reason"), route(config, task, items), strict=True)) for task in TASKS},
        "tasks": TASKS,
    }
