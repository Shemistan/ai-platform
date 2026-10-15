"""Движки и голоса narrator — как в его synth.py (ENGINES, ENGINE_VOICES, DEFAULT_VOICES)."""

from __future__ import annotations

from dataclasses import dataclass

CLONES = ("Charon", "M1", "Shem")  # клоны образцов narrator/voices


@dataclass(frozen=True)
class Engine:
    id: str
    label: str
    note: str
    voices: tuple[str, ...]
    default_voice: str
    tunable: bool = False  # слушает --speed

    def public(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "note": self.note,
            "voices": list(self.voices),
            "default_voice": self.default_voice,
            "speed": self.tunable,
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
SPEED_RANGE = (0.7, 2.0)  # как speed_arg в narrator/cli.py
