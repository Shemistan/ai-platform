"""Команда для `ltx-2-mlx` и разбор его вывода в прогресс.

Формат флагов и вывода сверен с исходниками ltx-2-mlx v0.16.2:
фазы печатаются как `[Label] ...` / `[Label] done in 1.2s`, шаги —
полосой tqdm `Denoising:  38%|███▊      | 3/8 [00:40<01:05, 13.2s/it]`.
"""

from __future__ import annotations

import re
from pathlib import Path

from aiplatform.video.timeline import FPS, GenerationSpec

KEYFRAME_STRENGTH = "1.0"

# Доля общего прогресса по проходам шумоподавления distilled-режима:
# 8 шагов на половинном разрешении, затем 3 шага доводки.
_PASS_RANGES = {1: (10.0, 60.0), 2: (60.0, 85.0)}
_AFTER_DENOISE = 85.0
_BEFORE_DENOISE_CAP = 9.0
_FINISHING_CAP = 99.0

_PHASE = re.compile(r"^\[(?P<label>[^\]]+)\]\s+(?P<rest>\.\.\.|done in .*)$")
_TQDM = re.compile(
    r"^(?P<desc>[^|]*?):?\s*(?P<pct>\d{1,3})%\|.*?\|\s*(?P<n>[\d.]+)(?P<unit>[kMGT]?B?)/(?P<total>[\d.]+)"
)

_LABELS = (
    ("Loading text encoder", "Загрузка текстового энкодера"),
    ("Encoding prompt", "Кодирование промпта"),
    ("Loading transformer", "Загрузка модели"),
    ("Loading decoders", "Загрузка декодеров"),
)


def build_argv(
    cmd: tuple[str, ...],
    spec: GenerationSpec,
    image_paths: list[Path],
    output: Path,
    *,
    model: str,
    gemma: str | None = None,
    low_ram: bool = False,
) -> list[str]:
    """argv для `ltx-2-mlx generate --distilled`. Кадры — в порядке spec.keyframes."""
    if len(image_paths) != len(spec.keyframes):
        raise ValueError("число картинок не совпадает с числом кадров")
    argv = [
        *cmd,
        "generate",
        "--distilled",
        "--model", model,
        f"--prompt={spec.prompt}",  # слитно: промпт может начинаться с дефиса
        "--output", str(output),
        "--height", str(spec.height),
        "--width", str(spec.width),
        "--frames", str(spec.num_frames),
        "--frame-rate", str(FPS),
        "--seed", str(spec.seed),
    ]  # fmt: skip
    if gemma:  # текстовый энкодер LTX-2.3 вне кэша Hugging Face; пакеты 2.5 берут свой
        argv += ["--gemma", gemma]
    if not spec.audio:
        argv.append("--no-audio")
    if low_ram:
        argv.append("--low-ram")
    for keyframe, path in zip(spec.keyframes, image_paths, strict=True):
        argv += ["--image", str(path), str(keyframe.frame_idx), KEYFRAME_STRENGTH]
    return argv


def _translate(label: str) -> str:
    for prefix, text in _LABELS:
        if label.startswith(prefix):
            return text
    return label


class ProgressParser:
    """Копит состояние прогресса по строкам вывода процесса."""

    def __init__(self) -> None:
        self.stage = "Запуск"
        self.step: int | None = None
        self.steps: int | None = None
        self.percent = 0.0
        self._pass = 0
        self._last_n: float | None = None  # последний n полосы Denoising; сброс вниз = новый проход

    def snapshot(self) -> dict:
        return {
            "stage": self.stage,
            "step": self.step,
            "steps": self.steps,
            "percent": round(self.percent, 1),
        }

    def _raise_percent(self, value: float) -> None:
        self.percent = max(self.percent, min(value, _FINISHING_CAP))

    def feed(self, line: str) -> bool:
        """Учесть строку. True — если это строка прогресса (в лог её не пишем)."""
        line = line.strip()
        if not line:
            return False

        phase = _PHASE.match(line)
        if phase:
            if phase["rest"] == "...":
                self.stage = _translate(phase["label"])
                self.step = self.steps = None
                if self._pass == 0:
                    self._raise_percent(min(self.percent + 2.0, _BEFORE_DENOISE_CAP))
                else:
                    self._raise_percent(self.percent + 3.0)
            return False

        bar = _TQDM.match(line)
        if not bar:
            return False
        desc = bar["desc"].strip()
        n, total = float(bar["n"]), float(bar["total"])
        if desc.startswith("Denoising"):
            if self._last_n is None or n < self._last_n:
                self._pass += 1
            self._last_n = n
            self.step, self.steps = int(n), int(total)
            lo, hi = _PASS_RANGES.get(self._pass, (_AFTER_DENOISE, _AFTER_DENOISE))
            self.stage = f"Шумоподавление, проход {self._pass}"
            self._raise_percent(lo + (hi - lo) * (n / total if total else 0.0))
        else:
            # Полосы скачивания весов и прочие: показываем, общий процент не двигаем.
            self.stage = f"{desc or 'Загрузка'} — {bar['pct']}%"
            self.step = self.steps = None
        return True
