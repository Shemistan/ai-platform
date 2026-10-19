"""Модели расшифровки (ASR) монтажа: каталог, установленность, команда воркера, разбор его вывода.

Веса — <AIP_MODELS_DIR>/asr/<имя репозитория>, ревизии закреплены (SEC-011): так их
качает tools/setup-model.sh (`make models M=<id>`). Воркер — montage/asr_worker.py в
окружении mlx-audio; протокол: строки `PROGRESS <процент> <этап>` в stdout, итог —
JSON в файле --out.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from aiplatform.montage.config import MontageConfig

MAX_CHUNK_S = 300.0  # кусок звука на один вызов модели: у выравнивателя Qwen предел ~5 мин
HOTWORDS_SEP = "|"
_PROGRESS = re.compile(r"^PROGRESS\s+(?P<pct>\d+(?:\.\d+)?)\s*(?P<stage>.*)$")


@dataclass(frozen=True)
class Weights:
    repo: str
    revision: str
    files: tuple[str, ...]  # признаки целой загрузки

    @property
    def dirname(self) -> str:
        return self.repo.split("/", 1)[1]


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    kind: str  # whisper | qwen3
    weights: Weights
    note: str
    size_gb: float
    aligner: Weights | None = None  # Qwen3-ASR: время слов даёт отдельный выравниватель


QWEN_FILES = ("config.json", "model.safetensors.index.json", "preprocessor_config.json", "vocab.json")

# Порядок = порядок в интерфейсе. Первая — по умолчанию (быстрая и точная на русской речи с латиницей).
CATALOG = (
    Model(
        "whisper-large-v3-turbo",
        "Whisper Large V3 Turbo",
        "whisper",
        Weights("mlx-community/whisper-large-v3-turbo", "a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb",
                ("config.json", "weights.safetensors")),
        "быстрая, хорошая точность; время слов — сама модель",
        1.5,
    ),
    Model(
        "whisper-large-v3",
        "Whisper Large V3",
        "whisper",
        Weights("mlx-community/whisper-large-v3-mlx", "49e6aa286ad60c14352c404340ded53710378a11",
                ("config.json", "weights.npz")),
        "полная, медленнее Turbo в несколько раз; точнее на редких словах",
        2.9,
    ),
    Model(
        "qwen3-asr-1.7b",
        "Qwen3-ASR 1.7B",
        "qwen3",
        Weights("mlx-community/Qwen3-ASR-1.7B-bf16", "e1f6c266914abc5a46e8756e02580f834a6cf8a7", QWEN_FILES),
        "сильна на смешанной речи; время слов — Qwen3-ForcedAligner 0.6B",
        5.5,
        aligner=Weights("mlx-community/Qwen3-ForcedAligner-0.6B-bf16", "53c8c0e46733eec430e4b53dd6471d0e5dee45f8",
                        QWEN_FILES),
    ),
)  # fmt: skip
DEFAULT_ID = CATALOG[0].id
# Язык речи — как его называет каждая модель.
LANGUAGE = {"whisper": "ru", "qwen3": "Russian"}


def get(model_id: str | None) -> Model | None:
    return next((m for m in CATALOG if m.id == model_id), None)


def weights_dir(config: MontageConfig, weights: Weights) -> Path:
    return config.asr_dir / weights.dirname


def _complete(config: MontageConfig, weights: Weights) -> bool:
    folder = weights_dir(config, weights)
    return all((folder / name).is_file() for name in weights.files)


def missing(model: Model, config: MontageConfig) -> list[str]:
    """Чего не хватает модели: окружения и/или весов. Пусто — установлена."""
    if config.fake:
        return []
    out = [] if config.asr_available() else [f"окружения mlx-audio ({config.asr_python})"]
    for weights in (model.weights, model.aligner):
        if weights is not None and not _complete(config, weights):
            out.append(f"весов {weights.repo} в {weights_dir(config, weights)}")
    return out


def installed(model: Model, config: MontageConfig) -> bool:
    return not missing(model, config)


def enabled(config: MontageConfig) -> list[Model]:
    """Каталог без выключенных во вкладке «Настройки»."""
    off = config.disabled_models()
    return [m for m in CATALOG if m.id not in off]


def default_id(config: MontageConfig) -> str:
    """Модель по умолчанию: первая включённая установленная, иначе первая включённая."""
    models = enabled(config)
    ready = [m for m in models if installed(m, config)]
    return (ready or models or list(CATALOG))[0].id


def public(config: MontageConfig) -> list[dict]:
    return [
        {
            "id": m.id,
            "label": m.label,
            "note": m.note,
            "size_gb": m.size_gb,
            "installed": installed(m, config),
        }
        for m in enabled(config)
    ]


def build_argv(
    config: MontageConfig,
    model: Model,
    audio: Path,
    out: Path,
    *,
    splits: list[float] | None = None,
    hotwords: list[str] | None = None,
) -> list[str]:
    argv = [
        *config.asr_cmd(),
        "--kind", model.kind,
        "--model", str(weights_dir(config, model.weights)),
        "--audio", str(audio),
        "--out", str(out),
        "--language", LANGUAGE[model.kind],
    ]  # fmt: skip
    if model.aligner is not None:
        argv += ["--aligner", str(weights_dir(config, model.aligner))]
    if splits:
        argv += ["--splits", ",".join(f"{s:.3f}" for s in splits)]
    words = [w.replace(HOTWORDS_SEP, " ").strip() for w in hotwords or []]
    words = [w for w in words if w]
    if words:
        argv += ["--hotwords", HOTWORDS_SEP.join(words)]
    return argv


def parse_progress(line: str) -> tuple[float, str] | None:
    """`PROGRESS 42.5 кусок 3 из 7` → (42.5, «кусок 3 из 7»); не строка прогресса — None."""
    found = _PROGRESS.match(line.strip())
    if not found:
        return None
    return min(100.0, float(found["pct"])), found["stage"].strip()


def catalog_lines() -> list[str]:
    """Каталог для tools/setup-model.sh: `id репозиторий ревизия [репозиторий ревизия]` — веса по порядку."""
    lines = []
    for model in CATALOG:
        parts = [model.id, model.weights.repo, model.weights.revision]
        if model.aligner is not None:
            parts += [model.aligner.repo, model.aligner.revision]
        lines.append(" ".join(parts))
    return lines


if __name__ == "__main__":
    print("\n".join(catalog_lines()))
