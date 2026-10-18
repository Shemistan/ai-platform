"""Журнал комплаенс-действий: append-only `<data>/audit.jsonl` (CMP-015).

При претензии (РКН, правообладатель) по нему видно, что ушло, куда, с какими
пометками и кто нажал. Строка — JSON: ts (ISO), at (unix), actor (local —
страница и API, scheduler — поток расписания, `claude -p` — агент), action и
поля действия: канал, message_id, sha256 подписи и медиа, сноски, версия
(дата сверки) реестра.

Действия: publish, registry_apply, voice_record, likeness_consent (лицо персонажа
«Диктора»), disclaimers_off, delete.
Файл только дополняется (права 0600); единственное исключение — ротация:
записи старше года уходят в `audit-archive.jsonl`, тоже дописыванием.
Механизм записи — тот же, что у журнала событий AN-002 (events.append_jsonl).
Сбой записи не роняет действие: журнал не должен останавливать публикацию.
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

from aiplatform.core import events

FILE = "audit.jsonl"
ARCHIVE = "audit-archive.jsonl"
KEEP_S = 365 * 24 * 3600  # записи старше — в архив
LOCAL, SCHEDULER, AGENT = "local", "scheduler", "claude -p"
ACTIONS = ("publish", "registry_apply", "voice_record", "likeness_consent", "disclaimers_off", "delete")
_ROTATE = threading.Lock()


def actor() -> str:
    """local — запрос со страницы или API; scheduler — поток расписания."""
    return SCHEDULER if events.actor() == events.SCHEDULER else LOCAL


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str | None:
    """sha256 файла; нет файла — None."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def footnotes(post: str) -> list[str]:
    """Сноски реестра из хвоста поста: строки «* …»."""
    return [line.strip()[2:].strip() for line in post.splitlines() if line.strip().startswith("* ")]


class AuditLog:
    def __init__(self, path: Path | None) -> None:
        self.path = path  # None — ничего не пишет (студия без журнала, тесты)

    def write(self, action: str, *, who: str | None = None, **fields) -> dict | None:
        """Дописать действие; поля None опускаются. Ошибка записи — None, без исключения."""
        now = time.time()
        record = {
            "ts": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "at": round(now, 3),
            "actor": who or actor(),
            "action": action,
            **{k: v for k, v in fields.items() if v is not None},
        }
        if self.path is None:
            return record
        return record if events.append_jsonl(self.path, record, mode=0o600) else None

    def read(self) -> list[dict]:
        """Все записи; битые строки пропускаются."""
        if self.path is None:
            return []
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(json.JSONDecodeError):
                item = json.loads(line)
                if isinstance(item, dict):
                    out.append(item)
        return out

    def rotate(self, now: float | None = None) -> int:
        """Записи старше года — в архив (дописыванием), в журнале — остальные. Возвращает, сколько ушло."""
        cutoff = (now or time.time()) - KEEP_S
        if self.path is None:
            return 0
        with _ROTATE, events.LOCK:
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
            except OSError:
                return 0
            old, keep = [], []
            for line in lines:
                try:
                    at = float(json.loads(line).get("at") or 0)
                except (ValueError, AttributeError, TypeError):
                    at = cutoff  # битая строка остаётся в журнале
                (old if at < cutoff else keep).append(line)
            if not old:
                return 0
            archive = self.path.with_name(ARCHIVE)
            fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as fh:
                fh.writelines(old)
            tmp = self.path.with_name(self.path.name + ".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.writelines(keep)
            tmp.replace(self.path)
            return len(old)
