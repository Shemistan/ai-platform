"""Каталог моделей диктора и то, какие из них установлены на этой машине."""

from __future__ import annotations

from dataclasses import dataclass

from aiplatform.avatar.config import AvatarConfig

# Файлы весов SadTalker в <AIP_MODELS_DIR>/SadTalker — как их качает tools/setup-model.sh.
SADTALKER_FILES = (
    "checkpoints/mapping_00109-model.pth.tar",
    "checkpoints/mapping_00229-model.pth.tar",
    "checkpoints/SadTalker_V0.0.2_256.safetensors",
    "checkpoints/SadTalker_V0.0.2_512.safetensors",
    "gfpgan/weights/alignment_WFLW_4HG.pth",
    "gfpgan/weights/detection_Resnet50_Final.pth",
    "gfpgan/weights/GFPGANv1.4.pth",
    "gfpgan/weights/parsing_parsenet.pth",
)


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    note: str
    sizes: tuple[int, ...]  # сторона кадра лица, px


# Порядок = порядок в интерфейсе; первая установленная выбирается по умолчанию.
CATALOG = (
    Model(
        "sadtalker",
        "SadTalker",
        "фото человека анфас; рисованный — только с лицом в человеческих пропорциях. На Mac идёт на процессоре: "
        "около 4 минут расчёта на секунду речи — длинный текст лучше ставить на ночь",
        (256, 512),
    ),
)
DEFAULT_ID = "sadtalker"


def installed(model: Model, config: AvatarConfig) -> bool:
    if config.fake:
        return True
    return config.runner_available() and all((config.weights / f).is_file() for f in SADTALKER_FILES)


def missing(config: AvatarConfig) -> list[str]:
    """Чего не хватает SadTalker: окружения и/или файлов весов (для problems)."""
    if config.fake:
        return []
    out = [] if config.runner_available() else [f"окружения в {config.sadtalker_home}"]
    if any(not (config.weights / f).is_file() for f in SADTALKER_FILES):
        out.append(f"весов в {config.weights}")
    return out


def public(config: AvatarConfig) -> list[dict]:
    return [
        {"id": m.id, "label": m.label, "note": m.note, "sizes": list(m.sizes), "installed": installed(m, config)}
        for m in CATALOG
    ]
