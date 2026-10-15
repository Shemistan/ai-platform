"""Команды `mflux-generate-*` и разбор их вывода в прогресс.

Флаги сверены с исходниками mflux 0.22.0. Шаги печатаются полосой tqdm без
подписи: `44%|████▍     | 4/9 [00:12<00:15,  3.05s/it]`; полосы скачивания
весов идут с подписью или с единицами (`1.2G/5.9G`).
"""

from __future__ import annotations

import re
from pathlib import Path

from aiplatform.photo.models import IMG2IMG, TXT2IMG, Model
from aiplatform.photo.params import GenerationSpec
from aiplatform.photo.progress import LOADING, Progress

# У mflux --image STRENGTH — доля шагов, которые пропускаются: сила исходника,
# а не изменений. Ноль выключил бы img2img совсем, поэтому снизу зажато.
_MIN_SOURCE_STRENGTH, _MAX_SOURCE_STRENGTH = 0.05, 0.9

_TQDM = re.compile(
    r"^(?P<desc>[^|]*?):?\s*(?P<pct>\d{1,3})%\|.*?\|\s*(?P<n>[\d.]+)(?P<unit>[kMGT]?B?)/(?P<total>[\d.]+)(?P<unit2>[kMGT]?B?)"
)


def source_strength(change: float) -> float:
    return round(min(max(1.0 - change, _MIN_SOURCE_STRENGTH), _MAX_SOURCE_STRENGTH), 2)


def build_argv(
    cmd: tuple[str, ...],
    model: Model,
    spec: GenerationSpec,
    output: Path,
    *,
    weights: str,
    source: Path | None = None,
    low_ram: bool = False,
) -> list[str]:
    """argv для команды mflux. cmd — как запустить `model.command`."""
    if spec.mode not in (TXT2IMG, IMG2IMG):
        raise ValueError(f"mflux не умеет режим {spec.mode}")
    argv = [*cmd, "--model", weights]
    if model.base_model:
        argv += ["--base-model", model.base_model]
    argv += [
        f"--prompt={spec.full_prompt()}",  # слитно: промпт может начинаться с дефиса
        "--width", str(spec.width),
        "--height", str(spec.height),
        "--seed", str(spec.seed),
        "--output", str(output),
    ]  # fmt: skip
    if spec.preset:
        argv += ["--preset", spec.preset]
    else:
        argv += ["--steps", str(spec.steps)]
    if model.guidance and spec.guidance is not None:
        argv += ["--guidance", f"{spec.guidance:g}"]
    if spec.negative_prompt:
        argv.append(f"--negative-prompt={spec.negative_prompt}")
    if spec.mode == IMG2IMG:
        if source is None or spec.strength is None:
            raise ValueError("img2img без исходника или силы изменений")
        argv += ["--image", str(source), f"{source_strength(spec.strength):g}"]
    if low_ram:
        argv.append("--low-ram")
    return argv


class ProgressParser(Progress):
    def __init__(self) -> None:
        super().__init__()
        self.set_stage("Загрузка модели", LOADING)

    def feed(self, line: str) -> bool:
        bar = _TQDM.match(line.strip())
        if not bar:
            return False
        desc = bar["desc"].strip()
        if desc or bar["unit"] or bar["unit2"]:
            # Скачивание весов и прочие полосы: показываем, общий процент не двигаем.
            self.set_stage(f"{desc or 'Загрузка весов'} — {bar['pct']}%")
        else:
            self.denoise(int(float(bar["n"])), int(float(bar["total"])))
        return True
