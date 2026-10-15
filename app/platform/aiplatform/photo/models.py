"""Каталог моделей: чем запускается, что умеет и что из этого установлено."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

TXT2IMG, IMG2IMG, INPAINT, OUTPAINT = "txt2img", "img2img", "inpaint", "outpaint"
MODES = (TXT2IMG, IMG2IMG, INPAINT, OUTPAINT)
DETAILS = ("draft", "standard", "high")

MFLUX, FLUX2 = "mflux", "flux2"
MARKER = ".photogen-installed"  # кладёт setup-model.sh после успешной загрузки
FLUX2_DIR = "flux2-swift-mlx"  # общий каталог весов Flux2CLI (--models-dir)
CACHE_DIR = ".photogen-cache"  # кэши пайплайнов рядом с весами: на системный диск ничего не пишем


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    backend: str  # MFLUX | FLUX2
    note: str
    modes: tuple[str, ...]
    steps: tuple[int, int, int]  # быстро / стандарт / максимум
    max_steps: int
    # mflux: команда и каталог весов (хвост репозитория mflux-community)
    command: str = ""
    weights: str = ""
    base_model: str = ""  # --base-model: нужен, когда --model — путь, а не имя
    presets: tuple[str, str, str] | None = None  # вместо шагов (Ideogram 4)
    # flux2: имя модели в Flux2CLI и квантизация под 32 ГБ
    variant: str = ""
    transformer_quant: str = ""
    text_quant: str = "4bit"
    # какие ручки модель слушает
    guidance: tuple[float, float, float] | None = None  # по умолчанию, мин, макс
    fixed_guidance: float | None = None  # передаётся как есть, в UI не выносится
    negative: bool = False
    strength_modes: tuple[str, ...] = ()
    edit_by_instruction: bool = False  # img2img понимает инструкцию, а не описание результата
    default_for: tuple[str, ...] = ()  # режимы, где модель предлагается первой


# Порядок = порядок в интерфейсе; первая установленная выбирается по умолчанию.
CATALOG = (
    Model(
        "flux2-dev", "FLUX.2 [dev]", FLUX2,
        "32B, самая тяжёлая: на 32 ГБ идёт на пределе памяти, минуты на шаг",
        modes=(TXT2IMG, IMG2IMG, INPAINT), steps=(16, 28, 40), max_steps=50,
        variant="dev", transformer_quant="int4",
        guidance=(4.0, 1.0, 10.0), strength_modes=(INPAINT,), edit_by_instruction=True,
    ),
    Model(
        "qwen-image-2512", "Qwen-Image-2512", MFLUX,
        "20B, сильна в тексте на картинке; медленная",
        modes=(TXT2IMG, IMG2IMG), steps=(12, 20, 30), max_steps=50,
        command="mflux-generate-qwen", weights="qwen-image-2512-mflux-q4", base_model="qwen-image",
        guidance=(3.5, 1.0, 10.0), negative=True, strength_modes=(IMG2IMG,),
    ),
    Model(
        "flux2-klein-9b", "FLUX.2 Klein 9B", FLUX2,
        "быстрая, умеет все четыре режима — лучший выбор для правки фото",
        modes=MODES, steps=(4, 4, 8), max_steps=12,
        variant="klein-9b", transformer_quant="qint8",
        fixed_guidance=1.0, strength_modes=(INPAINT,), edit_by_instruction=True,
        default_for=(IMG2IMG, INPAINT, OUTPAINT),
    ),
    Model(
        "z-image-turbo", "Z-Image Turbo", MFLUX,
        "6B, самая быстрая и лёгкая",
        modes=(TXT2IMG, IMG2IMG), steps=(6, 9, 12), max_steps=20,
        command="mflux-generate-z-image-turbo", weights="z-image-turbo-mflux-q8", base_model="z-image-turbo",
        strength_modes=(IMG2IMG,),
    ),
    Model(
        "krea-2", "Krea 2", MFLUX,
        "12B Turbo, фотореализм",
        modes=(TXT2IMG, IMG2IMG), steps=(6, 8, 12), max_steps=20,
        command="mflux-generate-krea2", weights="krea-2-turbo-mflux-q4",
        strength_modes=(IMG2IMG,),
    ),
    Model(
        "ideogram-4", "Ideogram 4", MFLUX,
        "9B, дизайн и типографика; только генерация по промпту",
        modes=(TXT2IMG,), steps=(12, 20, 48), max_steps=48,
        command="mflux-generate-ideogram4", weights="ideogram-4-mflux-q4",
        presets=("V4_TURBO_12", "V4_DEFAULT_20", "V4_QUALITY_48"),
    ),
)  # fmt: skip


@dataclass(frozen=True)
class Available:
    model: Model
    path: str  # mflux: каталог весов; flux2: общий каталог --models-dir
    installed: bool

    @property
    def id(self) -> str:
        return self.model.id

    @property
    def label(self) -> str:
        return self.model.label

    def public(self) -> dict:
        m = self.model
        return {
            "id": m.id,
            "label": m.label,
            "note": m.note,
            "installed": self.installed,
            "modes": list(m.modes),
            "steps": dict(zip(DETAILS, m.steps, strict=True)),
            "max_steps": m.max_steps,
            "custom_steps": m.presets is None,
            "guidance": dict(zip(("default", "min", "max"), m.guidance, strict=True)) if m.guidance else None,
            "negative": m.negative,
            "strength_modes": list(m.strength_modes),
            "edit_by_instruction": m.edit_by_instruction,
        }


def weights_dir(model: Model, models_dir: Path) -> Path:
    return models_dir / (FLUX2_DIR if model.backend == FLUX2 else model.weights)


def marker(model: Model, models_dir: Path) -> Path:
    """Отметка установки. У Flux2CLI каталог общий, поэтому отметка своя на модель."""
    name = f"{MARKER}-{model.variant}" if model.backend == FLUX2 else MARKER
    return weights_dir(model, models_dir) / name


def resolve(*, models_dir: Path | None, fake: bool) -> list[Available]:
    """Модели каталога с отметкой «установлена». В демо-режиме установлены все."""
    found = []
    for model in CATALOG:
        if models_dir is None:
            found.append(Available(model, "", fake))
            continue
        installed = fake or marker(model, models_dir).is_file()
        found.append(Available(model, str(weights_dir(model, models_dir)), installed))
    return found


def default(found: list[Available], mode: str = TXT2IMG) -> Available:
    """Модель по умолчанию для режима: установленная, по возможности — рекомендованная для него."""
    fits = [m for m in found if mode in m.model.modes]
    ready = [m for m in fits if m.installed]
    return next((m for m in ready if mode in m.model.default_for), (ready or fits)[0])


def setup_line(model: Model) -> str:
    """Строка для setup-model.sh: каталог моделей у скрипта и сервиса один.

    Поля: id, пайплайн, каталог весов, файл-отметка, источник, квантизация трансформера.
    """
    if model.backend == FLUX2:
        return f"{model.id} {FLUX2} {FLUX2_DIR} {MARKER}-{model.variant} {model.variant} {model.transformer_quant}"
    return f"{model.id} {MFLUX} {model.weights} {MARKER} mflux-community/{model.weights} -"


if __name__ == "__main__":
    print("\n".join(setup_line(m) for m in CATALOG))
