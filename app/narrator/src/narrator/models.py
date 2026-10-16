"""Где лежат модели: одна папка на внешнем диске, общая для всех проектов.

Папка — $NARRATOR_MODELS_DIR, по умолчанию /Volumes/External SSD/local_models.
Внутри у каждой модели свой каталог (имена — константы ниже). Скачивает их
туда `sh tools/fetch-models.sh`, переносит из ~/.cache — `sh tools/move-models.sh`.

Модели в новой папке нет, а на старом месте в ~/.cache она осталась — берём
оттуда: до переноса и без диска всё работает как раньше.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_ROOT = "/Volumes/External SSD/local_models"

ESPEECH = "ESpeech-TTS-1_RL-V2"
RUACCENT = "ruaccent"
SUPERTONIC = "supertonic3"
SILERO = "silero"
VOCOS = "vocos-mel-24khz"
WHISPER = "faster-whisper-small"
# Образцы голоса для клонирования, записанные человеком: ./narrate voice <имя>.
VOICES = "voices"


def models_root() -> Path:
    return Path(os.environ.get("NARRATOR_MODELS_DIR") or DEFAULT_ROOT).expanduser()


def locate(name: str, legacy: str | None = None, env: str | None = None) -> Path:
    """Путь к модели: переменная env, папка моделей, старое место — что найдётся первым."""
    explicit = os.environ.get(env) if env else None
    if explicit:
        return Path(explicit).expanduser()
    new = models_root() / name
    if legacy and not new.exists():
        old = Path(legacy).expanduser()
        if old.exists():
            return old
    return new


def missing_hint() -> str:
    """Что сказать человеку, когда модели нет на месте."""
    root = models_root()
    if not root.exists():
        return f"папки моделей {root} нет — подключи диск или задай $NARRATOR_MODELS_DIR"
    return "скачай один раз вне sandbox: sh tools/fetch-models.sh"
