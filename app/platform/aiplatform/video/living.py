"""«Живое фото»: одно фото → ролик, в котором оно стоит посередине.

Отдельной модели нет. Это обычная генерация LTX с единственным опорным кадром,
только кадр стоит не в начале, а на отметке 4 с восьмисекундного ролика: модель
сама дорисовывает и то, что было до снимка, и то, что после. Пайплайн такой
кадр принимает — у него любой кадр с позицией > 0 идёт как ключевой.
"""

from __future__ import annotations

from aiplatform.core.http import malformed
from aiplatform.video import models, timeline
from aiplatform.video.timeline import GenerationSpec, Keyframe, ValidationError

KIND = "living"
BEFORE_S = 4
AFTER_S = 4
DURATION_S = BEFORE_S + AFTER_S

# Размер кадра = ориентация + качество: пресет timeline с id «<ориентация>-<качество>».
ORIENTATIONS = {"landscape": "Горизонтально", "portrait": "Вертикально"}
QUALITIES = {"s": "Быстрое", "m": "Среднее", "l": "Высокое"}
DEFAULT_ORIENTATION = "landscape"
DEFAULT_QUALITY = "s"  # ролик длинный: на большом кадре генерация идёт в разы дольше и просит больше памяти

# Промпт у режима один на все фото: что на снимке, модель видит сама. Камера
# неподвижна, движения мелкие и на месте — так LTX меньше всего искажает людей.
PROMPT = (
    "A photograph comes to life, like a moving portrait. One continuous shot filmed with a completely "
    "static camera: the few seconds leading up to the moment the picture was taken and the few seconds "
    "right after it. The people and everything in the picture move naturally and subtly while staying "
    "in place: breathing, blinking, glancing around, changing expression, small gestures, hair and "
    "clothes stirring in a light breeze. The same people, the same setting, the same lighting and "
    "colors from the first frame to the last."
)


def suitable(available: list[models.Available]) -> list[models.Available]:
    """Модели, которые берут опорный кадр и ролик нужной длины, — установленные и нет."""
    return [
        m for m in available
        if m.pipeline == "ltx" and m.limits.max_keyframes != 0 and DURATION_S in m.limits.durations_s
    ]  # fmt: skip


def pick_model(available: list[models.Available], model_id: object = None) -> models.Available:
    """Модель по id из запроса; пусто — первая установленная из подходящих."""
    if model_id is not None and not isinstance(model_id, str):
        raise ValidationError(malformed("model должен быть строкой — id модели."))
    fitting = suitable(available)
    if not model_id:
        model = models.default(fitting) if any(m.installed for m in fitting) else None
        if model is None:
            raise ValidationError("Живому фото нужна модель LTX: запусти make models M=ltx-2.3-q4.")
        return model
    model = next((m for m in available if m.id == model_id), None)
    if model is None:
        raise ValidationError(models.unknown(model_id))
    if model not in fitting:
        raise ValidationError(f"Модель {model.label} не умеет оживлять фото: выбери LTX.")
    if not model.installed:
        raise ValidationError(f"Модель {model.label} не установлена: запусти make models M={model.id}.")
    return model


def build_spec(payload: object, model: models.Available) -> GenerationSpec:
    if not isinstance(payload, dict):
        raise ValidationError("Тело запроса должно быть JSON-объектом.")
    upload_id = payload.get("upload_id")
    if not isinstance(upload_id, str) or not upload_id:
        raise ValidationError("Загрузи фото, которое нужно оживить.")
    orientation = payload.get("orientation", DEFAULT_ORIENTATION)
    if orientation not in ORIENTATIONS:
        raise ValidationError("Ориентация — landscape (горизонтально) или portrait (вертикально).")
    quality = payload.get("quality", DEFAULT_QUALITY)
    if quality not in QUALITIES:
        raise ValidationError(f"Качество — одно из: {', '.join(QUALITIES)}.")
    preset = f"{orientation}-{quality}"
    width, height, _ = timeline.PRESETS[preset]
    fps = model.limits.fps
    return GenerationSpec(
        prompt=PROMPT,
        width=width,
        height=height,
        num_frames=timeline.frames_for_duration(DURATION_S, fps),
        duration_s=DURATION_S,
        preset=preset,
        seed=-1,
        audio=False,  # звук по общему промпту вышел бы случайным шумом
        keyframes=(Keyframe(upload_id, BEFORE_S * fps),),
        model=model.id,
        fps=fps,
        kind=KIND,
    )


def describe(available: list[models.Available]) -> dict:
    try:
        default, problem = pick_model(available).id, None
    except ValidationError as exc:
        default, problem = None, str(exc)
    return {
        "before_s": BEFORE_S,
        "after_s": AFTER_S,
        "models": [
            {"id": m.id, "label": m.label, "note": m.note, "installed": m.installed} for m in suitable(available)
        ],
        "default_model": default,
        "orientations": [{"id": key, "label": label} for key, label in ORIENTATIONS.items()],
        "default_orientation": DEFAULT_ORIENTATION,
        "qualities": [
            {"id": key, "label": label, "sizes": {o: list(timeline.PRESETS[f"{o}-{key}"][:2]) for o in ORIENTATIONS}}
            for key, label in QUALITIES.items()
        ],
        "default_quality": DEFAULT_QUALITY,
        "problem": problem,
    }
