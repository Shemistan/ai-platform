"""Движки и голоса narrator — как в его synth.py (ENGINES, ENGINE_VOICES, DEFAULT_VOICES)."""

from __future__ import annotations

from dataclasses import dataclass, replace

from aiplatform.core.settings import Settings

CLONES = ("Charon", "M1", "Shem", "Shem-v2")  # клоны образцов narrator/voices


@dataclass(frozen=True)
class Engine:
    id: str
    label: str
    note: str
    voices: tuple[str, ...]
    default_voice: str
    tunable: bool = False  # слушает --speed
    license_nc: bool = False  # некоммерческая лицензия весов: только черновики, не публикация (CMP-018)

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.note,
            "voices": list(self.voices),
            "default_voice": self.default_voice,
            "speed": self.tunable,
            "license_nc": self.license_nc,
        }


ENGINES: dict[str, Engine] = {
    e.id: e
    for e in (
        Engine(
            "espeech", "ESpeech", "лучшее качество; нужна видеокарта, ~0,4× реального времени", CLONES, "Charon", True
        ),
        Engine(
            "silero",
            "Silero",
            "очень быстро на CPU; лицензия CC BY-NC — для черновиков",
            ("eugene", "aidar", "baya", "kseniya", "xenia"),
            "eugene",
            license_nc=True,
        ),
        Engine(
            "supertonic",
            "Supertonic",
            "быстро на CPU, 10 голосов",
            tuple(f"{g}{n}" for g in "FM" for n in range(1, 6)),
            "M1",
            True,
        ),
        Engine("higgs", "Higgs (MLX)", "клон голоса на Apple Silicon; движок дайджеста", CLONES, "Charon"),
        Engine("fish", "Fish (MLX)", "клон голоса на Apple Silicon", CLONES, "Charon"),
        Engine("moss", "MOSS (MLX)", "клон голоса на Apple Silicon", CLONES, "Charon"),
        Engine("qwen3", "Qwen3 TTS (MLX)", "клон голоса на Apple Silicon", CLONES, "Charon"),
        Engine("chatterbox", "Chatterbox (MLX)", "клон голоса на Apple Silicon", CLONES, "Charon"),
    )
}
DEFAULT_ENGINE = "higgs"  # голос по умолчанию у Higgs — Charon
# Движок не задан явно — берётся первый с установленными весами в этом порядке:
# higgs ставит `make models-mlx E=higgs`, espeech и silero — `make models`.
FALLBACK_ORDER = ("higgs", "espeech", "silero")
MLX_ENGINES = ("higgs", "fish", "moss", "qwen3", "chatterbox")  # веса ставит make models-mlx


def models_command(engine_id: str) -> str:
    """Какой командой поставить веса движка."""
    if engine_id in MLX_ENGINES:
        return f"cd app/narrator && make models-mlx E={engine_id}"
    return "cd app/narrator && make models"


SPEED_RANGE = (0.7, 2.0)  # как speed_arg в narrator/cli.py


def enabled(settings: Settings | None) -> dict[str, Engine]:
    """Движки и голоса без выключенных в настройках; голос по умолчанию — первый оставшийся."""
    if settings is None:
        return dict(ENGINES)
    data = settings.load()["disabled"]
    off_engines, off_voices = set(data["audio"]), data["voices"]
    out = {}
    for engine in ENGINES.values():
        names = tuple(v for v in engine.voices if v not in off_voices.get(engine.id, ()))
        if engine.id in off_engines or not names:
            continue
        default_voice = engine.default_voice if engine.default_voice in names else names[0]
        out[engine.id] = replace(engine, voices=names, default_voice=default_voice)
    return out


def unknown(engine_id: object, voice: object = None) -> str:
    """Почему движка или голоса нет в списке: выключен в настройках или такого нет вовсе."""
    engine = ENGINES.get(engine_id) if isinstance(engine_id, str) else None
    if engine is None:
        return "Неизвестный движок озвучки."
    if voice is None:
        return f"Движок {engine.label} выключен — включи его во вкладке «Настройки»."
    if voice in engine.voices:
        return f"Голос {voice} у движка {engine.label} выключен — включи его во вкладке «Настройки»."
    return f"У движка {engine.label} нет голоса {voice!r}."
