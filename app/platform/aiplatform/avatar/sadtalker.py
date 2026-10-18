"""Команда для SadTalker `inference.py` и разбор его вывода в прогресс.

Флаги и вывод сверены с inference.py ревизии cd4c046: этапы идут полосами tqdm
(`landmark Det:`, `3DMM Extraction In Video:`, `mel:`, `audio2exp:`,
`Face Renderer:`, `seamlessClone:`, `Face Enhancer:`), итог — строкой
`The generated video is named: <путь>.mp4`. Лица на картинке не нашлось —
`Can't get the coeffs of the input` и выход с кодом 0 без видео.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from aiplatform.video.ltx import _TQDM

NO_FACE = "Can't get the coeffs of the input"
RESULT = re.compile(r"The generated video is named:\s*(?P<path>.+\.mp4)\s*$")
_FINISHING_CAP = 99.0

# Кадр: «Голова крупно» — лицо вырезается и анимируется само; «Как на фото» — лицо
# оживает внутри исходного снимка, голова почти не поворачивается (--still).
FRAMES = {"crop": "Голова крупно", "full": "Как на фото"}
DEFAULT_FRAME = "crop"

# Полоса tqdm → (этап, доля общего прогресса от, до). Без улучшения лица Face Renderer доходит до конца.
_BARS = (
    ("landmark Det", "Поиск лица", 2.0, 8.0),
    ("3DMM Extraction", "Разбор лица", 8.0, 15.0),
    ("mel", "Разбор речи", 15.0, 18.0),
    ("audio2exp", "Мимика по речи", 18.0, 25.0),
    ("Face Renderer", "Анимация", 25.0, 90.0),
    ("seamlessClone", "Вклейка в фото", 90.0, 95.0),
    ("Face Enhancer", "Улучшение лица", 95.0, 99.0),
)


@dataclass(frozen=True)
class RenderSpec:
    audio: Path  # wav
    image: Path
    size: int
    frame: str
    enhance: bool


def build_argv(cmd: tuple[str, ...], spec: RenderSpec, result_dir: Path, weights: Path) -> list[str]:
    argv = [
        *cmd,
        "--driven_audio", str(spec.audio),
        "--source_image", str(spec.image),
        "--result_dir", str(result_dir),
        "--checkpoint_dir", str(weights / "checkpoints"),
        "--size", str(spec.size),
        "--preprocess", spec.frame,
        "--cpu",  # CUDA на Mac нет; явно — чтобы не зависеть от проверки внутри
    ]  # fmt: skip
    if spec.frame == "full":
        argv.append("--still")
    if spec.enhance:
        argv += ["--enhancer", "gfpgan"]
    return argv


class ProgressParser:
    """Копит состояние прогресса по строкам вывода; помнит итоговый файл и «лица нет»."""

    def __init__(self) -> None:
        self.stage = "Запуск"
        self.step: int | None = None
        self.steps: int | None = None
        self.percent = 0.0
        self.result: Path | None = None
        self.no_face = False

    def snapshot(self) -> dict:
        return {"stage": self.stage, "step": self.step, "steps": self.steps, "percent": round(self.percent, 1)}

    def feed(self, line: str) -> bool:
        """Учесть строку. True — строка прогресса (в лог её не пишем)."""
        line = line.strip()
        if not line:
            return False
        if NO_FACE in line:
            self.no_face = True
            return False
        if found := RESULT.search(line):
            self.result = Path(found["path"].strip())
            return False
        bar = _TQDM.match(line)
        if not bar:
            return False
        desc = bar["desc"].strip()
        n, total = float(bar["n"]), float(bar["total"])
        for prefix, stage, lo, hi in _BARS:
            if desc.startswith(prefix):
                self.stage = stage
                self.step, self.steps = int(n), int(total)
                share = n / total if total else 0.0
                self.percent = max(self.percent, min(lo + (hi - lo) * share, _FINISHING_CAP))
                return True
        return True
