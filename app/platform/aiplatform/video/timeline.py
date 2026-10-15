"""Проверка запроса на генерацию: длительность, размер кадра, позиции кадров.

Чистые функции: ни диска, ни сети. Ограничения — от пайплайнов: у LTX число
кадров на сетке 8k+1 при 24 кадрах/с, у Wan — 4k+1 при 16; стороны кратны 64.
Что допускает конкретная модель, описывает Limits.
"""

from __future__ import annotations

from dataclasses import dataclass

FPS = 24
MIN_KEYFRAME_GAP = 8  # один латентный кадр
MAX_PROMPT_LEN = 4000
DURATIONS_S = (2, 3, 4, 5, 6, 8)
DEFAULT_DURATION_S = 4

# id → (ширина, высота, подпись)
PRESETS: dict[str, tuple[int, int, str]] = {
    "landscape-s": (512, 320, "Горизонтальное 512×320 — быстро"),
    "landscape-m": (704, 448, "Горизонтальное 704×448"),
    "landscape-l": (960, 576, "Горизонтальное 960×576 — медленно"),
    "portrait-s": (320, 512, "Вертикальное 320×512 — быстро"),
    "portrait-m": (448, 704, "Вертикальное 448×704"),
    "portrait-l": (576, 960, "Вертикальное 576×960 — медленно"),
    "square-s": (512, 512, "Квадрат 512×512"),
}
DEFAULT_PRESET = "landscape-s"


class ValidationError(ValueError):
    """Запрос не годится; текст показывается пользователю."""


@dataclass(frozen=True)
class Limits:
    """Что принимает модель: частота кадров, длины ролика, опорные кадры, звук."""

    fps: int = FPS
    durations_s: tuple[int, ...] = DURATIONS_S
    max_keyframes: int | None = None  # None — сколько помещается на шкале; 0 — только промпт
    audio: bool = True

    def public(self) -> dict:
        return {
            "fps": self.fps,
            "durations_s": list(self.durations_s),
            "max_keyframes": self.max_keyframes,
            "audio": self.audio,
        }


LTX = Limits()


@dataclass(frozen=True)
class Keyframe:
    upload_id: str
    frame_idx: int


@dataclass(frozen=True)
class GenerationSpec:
    prompt: str
    width: int
    height: int
    num_frames: int
    duration_s: int
    preset: str
    seed: int
    audio: bool
    keyframes: tuple[Keyframe, ...]
    model: str = ""  # id модели из каталога; пусто — модель по умолчанию
    fps: int = FPS
    kind: str = "video"  # "living" — живое фото, см. living.py


def frames_for_duration(duration_s: int, fps: int = FPS) -> int:
    """Секунды → число кадров: 8k+1 при 24 кадрах/с (4 с → 97), 4k+1 при 16 (4 с → 65)."""
    return duration_s * fps + 1


def max_keyframes(num_frames: int) -> int:
    return (num_frames - 1) // MIN_KEYFRAME_GAP + 1


def distribute(count: int, num_frames: int) -> list[int]:
    """Равномерные позиции: первый кадр на 0, последний — на конце ролика."""
    if count <= 0:
        return []
    if count == 1:
        return [0]
    last = num_frames - 1
    return [round(i * last / (count - 1)) for i in range(count)]


def _positions(frames: list[dict], num_frames: int, fps: int) -> list[int]:
    given = [f.get("frame_idx") for f in frames]
    if all(g is None for g in given):
        return distribute(len(frames), num_frames)
    if any(g is None for g in given):
        raise ValidationError("Позиция задана не у всех кадров: задай у всех или ни у одного.")
    for g in given:
        if isinstance(g, bool) or not isinstance(g, int):
            raise ValidationError("Позиция кадра должна быть целым числом.")
    if given[0] != 0:
        raise ValidationError("Первый кадр — начальный, его позиция должна быть 0.")
    for n, (prev, cur) in enumerate(zip(given, given[1:], strict=False), start=2):
        if cur - prev < MIN_KEYFRAME_GAP:
            raise ValidationError(
                f"Кадр {n} стоит слишком близко к кадру {n - 1} или раньше него: "
                f"между соседними кадрами нужно не меньше {MIN_KEYFRAME_GAP / fps:.2f} с."
            )
    if given[-1] > num_frames - 1:
        raise ValidationError("Последний кадр стоит за концом ролика.")
    return given


def build_spec(payload: object, limits: Limits = LTX) -> GenerationSpec:
    if not isinstance(payload, dict):
        raise ValidationError("Тело запроса должно быть JSON-объектом.")

    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError("Напиши промпт — описание того, что должно происходить в видео.")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT_LEN:
        raise ValidationError(f"Промпт длиннее {MAX_PROMPT_LEN} символов.")

    duration_s = payload.get("duration_s", DEFAULT_DURATION_S)
    if duration_s not in limits.durations_s:
        raise ValidationError(f"Длительность для этой модели: одно из {', '.join(map(str, limits.durations_s))} с.")
    num_frames = frames_for_duration(duration_s, limits.fps)

    preset = payload.get("preset", DEFAULT_PRESET)
    if preset not in PRESETS:
        raise ValidationError("Неизвестный размер кадра.")
    width, height, _ = PRESETS[preset]

    seed = payload.get("seed")
    if seed is None or seed == "":
        seed = -1
    if isinstance(seed, bool) or not isinstance(seed, int) or not -1 <= seed < 2**31:
        raise ValidationError("Seed — целое число от 0 до 2147483647 или пусто.")

    model = payload.get("model") or ""
    if not isinstance(model, str):
        raise ValidationError("model должен быть строкой — id модели.")

    frames = payload.get("frames") or []
    if not isinstance(frames, list) or not all(isinstance(f, dict) for f in frames):
        raise ValidationError("frames должен быть списком объектов.")
    if frames and limits.max_keyframes == 0:
        raise ValidationError("Эта модель работает только по промпту: убери кадры или выбери другую модель.")
    if limits.max_keyframes is not None and len(frames) > limits.max_keyframes:
        raise ValidationError(f"Эта модель принимает не больше {limits.max_keyframes} кадров.")
    if len(frames) > max_keyframes(num_frames):
        raise ValidationError(
            f"Слишком много кадров для ролика в {duration_s} с: не больше {max_keyframes(num_frames)}."
        )
    ids = [f.get("upload_id") for f in frames]
    if not all(isinstance(i, str) and i for i in ids):
        raise ValidationError("У каждого кадра должен быть upload_id.")
    positions = _positions(frames, num_frames, limits.fps)

    return GenerationSpec(
        prompt=prompt,
        width=width,
        height=height,
        num_frames=num_frames,
        duration_s=duration_s,
        preset=preset,
        seed=seed,
        audio=limits.audio and bool(payload.get("audio", True)),
        keyframes=tuple(Keyframe(i, p) for i, p in zip(ids, positions, strict=True)),
        model=model,
        fps=limits.fps,
    )
