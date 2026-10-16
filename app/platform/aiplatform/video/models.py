"""Каталог моделей и то, какие из них установлены на этой машине."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiplatform.video import timeline


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    repo: str  # репозиторий весов на Hugging Face; имя каталога весов = хвост репозитория
    note: str = ""
    pipeline: str = "ltx"  # чем запускать: "ltx" — ltx-2-mlx, "wan" — pocketai-wan-video
    subdir: str = ""  # подкаталог весов, который передаётся пайплайну
    limits: timeline.Limits = timeline.LTX


# Порядок = порядок в интерфейсе; первая установленная выбирается по умолчанию.
CATALOG = (
    Model("ltx-2.3-q4", "LTX-2.3 Q4", "dgrauet/ltx-2.3-mlx-q4", "быстрее и легче"),
    Model("ltx-2.5-q4", "LTX-2.5 Q4", "dgrauet/ltx-2.5-mlx-q4", "новее; доступ к весам — после принятия лицензии"),
    Model(
        "wan-2.2-t2v-q4",
        "Wan 2.2 T2V A14B Q4",
        "PocketAiHub/Wan2.2-T2V-A14B-MLX",
        "только по промпту: без кадров и звука, 16 кадров/с, до 5 с; заметно медленнее LTX",
        pipeline="wan",
        subdir="q4",
        # Модель обучена на 81 кадре при 16 кадрах/с — длиннее 5 с не берём.
        limits=timeline.Limits(fps=16, durations_s=(2, 3, 4, 5), max_keyframes=0, audio=False),
    ),
)
CUSTOM_ID = "custom"
GEMMA_DIR = "gemma-3-12b-it-4bit"  # сюда setup-model.sh кладёт текстовый энкодер LTX-2.3


@dataclass(frozen=True)
class Available:
    id: str
    label: str
    note: str
    path: str  # что передаётся пайплайну: каталог весов или репозиторий
    installed: bool
    pipeline: str = "ltx"
    limits: timeline.Limits = timeline.LTX

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.note,
            "installed": self.installed,
            **self.limits.public(),
        }


def resolve(*, models_dir: Path | None, explicit: str, fake: bool) -> list[Available]:
    """Модели каталога с отметкой «установлена».

    explicit — значение VIDEOGEN_MODEL: каталог весов или репозиторий. Если оно не
    совпадает ни с одной моделью каталога, добавляется первой строкой как «своя».
    """
    found: list[Available] = []
    explicit_dir = Path(explicit).expanduser() if explicit else None
    matched = not explicit
    for model in CATALOG:
        local = models_dir / model.repo.rsplit("/", 1)[1] / model.subdir if models_dir else None
        if explicit == model.repo and model.pipeline == "ltx":
            matched, path, installed = True, model.repo, True  # пайплайн скачает репозиторий сам
        elif local is not None and local.is_dir():
            matched = matched or (explicit_dir is not None and explicit_dir.resolve() == local.resolve())
            path, installed = str(local), True
        else:
            path, installed = model.repo, fake
        found.append(Available(model.id, model.label, model.note, path, installed, model.pipeline, model.limits))
    if not matched:
        name = explicit_dir.name if explicit_dir and explicit_dir.is_dir() else explicit
        found.insert(0, Available(CUSTOM_ID, name, "задана в VIDEOGEN_MODEL", explicit, True))
    return found


def unknown(model_id: object, missing: str = "Неизвестная модель.") -> str:
    """Почему модели нет в списке: выключена в настройках или такой нет вовсе."""
    model = next((m for m in CATALOG if m.id == model_id), None)
    if model:
        return f"Модель {model.label} выключена — включи её во вкладке «Настройки»."
    return missing


DEFAULT_ID = "ltx-2.5-q4"  # модель по умолчанию везде: вкладка «Видео», живое фото, ролики сценариев


def default(found: list[Available]) -> Available | None:
    """Заданная в VIDEOGEN_MODEL, иначе DEFAULT_ID, иначе первая установленная; None — все выключены."""
    installed = [m for m in found if m.installed]
    preferred = [m for m in installed if m.id in (CUSTOM_ID, DEFAULT_ID)]
    return (preferred or installed or found or [None])[0]
