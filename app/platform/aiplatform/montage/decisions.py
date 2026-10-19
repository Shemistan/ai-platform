"""Журнал решений проекта (MASTER_SPEC §6, §10): кто, что, по какой версии промпта и с какими хэшами.

`decisions.jsonl` в каталоге проекта, строка на решение: роль (план, проверка, знания, биты, стиль,
раскадровка, ворота), модель (команда LLM), версия промпта (sha256 текста промпта — меняется
с правилами и уроками), хэши входа и выхода, уверенность, обоснование, затронутые id. Файл
append-only: это provenance, его читает экспорт.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

FILE = "decisions.jsonl"
MAX_RATIONALE = 500


def sha(value: object) -> str:
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def record(
    folder: Path,
    role: str,
    action: str,
    *,
    model: str = "",
    prompt: str | None = None,
    inputs: object = None,
    output: object = None,
    confidence: float | None = None,
    rationale: str = "",
    ids: list[str] | tuple[str, ...] = (),
    auto: bool = False,
) -> dict:
    entry = {
        "at": round(time.time(), 3),
        "role": role,
        "action": action,
        "model": model,
        "prompt_version": sha(prompt.encode("utf-8"))[:12] if prompt else None,
        "input_hash": sha(inputs)[:16] if inputs is not None else None,
        "output_hash": sha(output)[:16] if output is not None else None,
        "confidence": confidence,
        "rationale": " ".join(rationale.split())[:MAX_RATIONALE],
        "ids": list(ids),
        "auto": auto,
    }
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(folder: Path, limit: int | None = None) -> list[dict]:
    path = folder / FILE
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out[-limit:] if limit else out
