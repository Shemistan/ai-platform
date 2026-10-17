"""Журнал комплаенс-действий narrator: append-only `audit.jsonl` рядом с telegram.json (CMP-015).

Тот же формат, что у платформы (aiplatform/core/audit.py): ts, at, actor,
action и поля. narrator пишет отправки в Telegram: что ушло (sha256 подписи и
медиа), куда, со сносками реестра и версией (датой сверки) реестра. Файл
только дополняется, права 0600; сбой записи отправку не роняет.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path

FILE = "audit.jsonl"
AUDIT_ENV = "NARRATOR_AUDIT"  # свой путь к журналу (тесты); иначе рядом с telegram.json
ACTOR_ENV = (
    "NARRATOR_ACTOR"  # кто запустил: платформа ставит local или scheduler; агент — claude -p
)
_LOCK = threading.Lock()


def actor() -> str:
    return os.environ.get(ACTOR_ENV, "").strip() or "local"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str | None:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def footnotes(caption: str) -> list[str]:
    """Сноски реестра из подписи: строки «* …»."""
    return [s.strip()[2:].strip() for s in caption.splitlines() if s.strip().startswith("* ")]


def path_for(root: Path) -> Path:
    own = os.environ.get(AUDIT_ENV, "").strip()
    return Path(own).expanduser() if own else root / FILE


def write(root: Path, action: str, **fields) -> dict | None:
    """Дописать действие в root/audit.jsonl; поля None опускаются. Ошибка — None."""
    now = time.time()
    record = {
        "ts": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "at": round(now, 3),
        "actor": actor(),
        "action": action,
        **{k: v for k, v in fields.items() if v is not None},
    }
    path = path_for(root)
    try:
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with _LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.write(line)
    except Exception:  # noqa: BLE001 — журнал не должен ронять отправку
        return None
    return record


def read(root: Path) -> list[dict]:
    try:
        lines = path_for(root).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        with contextlib.suppress(json.JSONDecodeError):
            item = json.loads(line)
            if isinstance(item, dict):
                out.append(item)
    return out
