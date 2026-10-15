"""Команды `Flux2CLI` (flux-2-swift-mlx) и разбор его вывода в прогресс.

Флаги сверены с `--help` бинаря v3.1.0. Формат вывода взят из строк бинаря
(`Step %ld/%ld`, `Loading transformer...`, `Denoising complete, decoding
image...`) и на живой генерации не проверен — незнакомые строки просто уходят
в лог.
"""

from __future__ import annotations

import re
from pathlib import Path

from aiplatform.photo.models import IMG2IMG, INPAINT, OUTPAINT, TXT2IMG, Model
from aiplatform.photo.params import GenerationSpec
from aiplatform.photo.progress import DECODING, FINISHING_CAP, LOADING, Progress

_STEP = re.compile(r"\bStep\s+(\d+)\s*/\s*(\d+)")
_STAGES = (
    ("Downloading", "Скачивание весов", None),
    ("Loading text encoder", "Загрузка текстового энкодера", LOADING),
    ("Encoding prompt", "Кодирование промпта", LOADING + 2),
    ("Loading transformer", "Загрузка модели", LOADING + 4),
    ("Denoising complete", "Декодирование и сохранение", DECODING),
    ("Output saved to", "Сохранение", FINISHING_CAP),
)


def build_argv(
    cmd: tuple[str, ...],
    model: Model,
    spec: GenerationSpec,
    output: Path,
    *,
    models_dir: str,
    source: Path | None = None,
    mask: Path | None = None,
    low_ram: bool = False,
) -> list[str]:
    """argv для Flux2CLI. Подкоманда зависит от режима; флаги у подкоманд разные."""
    if spec.mode != TXT2IMG and source is None:
        raise ValueError(f"{spec.mode} без исходника")
    prompt = spec.full_prompt()
    sampling = ["--steps", str(spec.steps), "--guidance", f"{spec.guidance:g}", "--seed", str(spec.seed)]
    quant = ["--text-quant", model.text_quant, "--transformer-quant", model.transformer_quant]
    size = ["--width", str(spec.width), "--height", str(spec.height)]
    common = ["--output", str(output), "--models-dir", models_dir]

    if spec.mode in (TXT2IMG, IMG2IMG):
        argv = [*cmd, "t2i" if spec.mode == TXT2IMG else "i2i", "--model", model.variant]
        if spec.mode == IMG2IMG:
            argv += ["--images", str(source)]
        argv += [*size, *sampling, *quant, *common]
        if low_ram:
            argv += ["--memory-profile", "conservative"]
        return [*argv, "--", prompt]  # промпт позиционный: после «--» может начинаться с дефиса

    argv = [*cmd, spec.mode, "--flux-model", model.variant, "--image", str(source), f"--prompt={prompt}"]
    if spec.mode == INPAINT:
        if mask is None or spec.strength is None:
            raise ValueError("inpaint без маски или силы изменений")
        argv += [
            "--mask", str(mask),
            "--strength", f"{spec.strength:g}",
            # Рабочее разрешение = размер исходника; нетронутые пиксели остаются бит-в-бит.
            "--max-pixels", str(spec.width * spec.height),
            "--composite-on-original",
            *quant,
        ]  # fmt: skip
    elif spec.mode == OUTPAINT:
        p = spec.padding
        argv += ["--left", str(p.left), "--top", str(p.top), "--right", str(p.right), "--bottom", str(p.bottom)]
    else:
        raise ValueError(f"неизвестный режим {spec.mode}")
    return [*argv, *sampling, *common]


class ProgressParser(Progress):
    def feed(self, line: str) -> bool:
        line = line.strip()
        step = _STEP.search(line)
        if step:
            self.denoise(int(step[1]), int(step[2]))
            return True
        for marker, stage, percent in _STAGES:
            if marker in line:
                self.set_stage(stage, percent)
                break
        return False
