"""Проверка запроса на генерацию: режим, модель, размеры, стиль, детализация."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from aiplatform.photo import models
from aiplatform.photo.models import IMG2IMG, INPAINT, OUTPAINT, TXT2IMG, Available

MAX_PROMPT = 4000
GRID = 32  # кратность сторон: общий знаменатель mflux (16) и Flux2CLI (32)
MIN_SIDE, MAX_SIDE = 256, 2048
MAX_SEED = 2**31 - 1

# Размеры для генерации по промпту: (ширина, высота, подпись).
SIZES = {
    "1024x1024": (1024, 1024, "1024×1024 — квадрат"),
    "1216x832": (1216, 832, "1216×832 — альбом 3:2"),
    "832x1216": (832, 1216, "832×1216 — портрет 2:3"),
    "1344x768": (1344, 768, "1344×768 — широкий 16:9"),
    "768x1344": (768, 1344, "768×1344 — вертикаль 9:16"),
    "768x768": (768, 768, "768×768 — квадрат, быстрее"),
    "512x512": (512, 512, "512×512 — черновик"),
}
DEFAULT_SIZE = "1024x1024"

# Рабочий размер для правки фото: длинная сторона, к которой браузер приводит исходник.
WORK_SIDES = (768, 1024, 1536)
DEFAULT_WORK_SIDE = 1024

# Стиль — добавка к промпту: (подпись, текст). Модели обучены на английских подписях.
STYLES = {
    "none": ("Без стиля", ""),
    "photo": ("Фотография", "photorealistic photograph, natural lighting, sharp focus"),
    "cinematic": ("Кино", "cinematic film still, dramatic lighting, shallow depth of field, color graded"),
    "studio": ("Студийный портрет", "professional studio portrait, softbox lighting, clean backdrop, 85mm lens"),
    "anime": ("Аниме", "anime illustration, clean line art, vibrant colors"),
    "digital-art": ("Цифровая живопись", "digital painting, concept art, detailed brushwork"),
    "oil": ("Масло", "oil painting on canvas, visible brush strokes, rich texture"),
    "watercolor": ("Акварель", "watercolor painting, soft washes, paper texture"),
    "3d": ("3D-рендер", "3D render, physically based materials, global illumination"),
    "pixel-art": ("Пиксель-арт", "pixel art, limited palette, crisp pixels"),
    "sketch": ("Скетч", "pencil sketch, hand-drawn, cross-hatching"),
}
DEFAULT_STYLE = "none"

DETAIL_LABELS = {"draft": "Быстро", "standard": "Стандарт", "high": "Максимум"}
DEFAULT_DETAIL = "standard"
DEFAULT_STRENGTH = {IMG2IMG: 0.6, INPAINT: 1.0}
MIN_STRENGTH = 0.1

MODE_LABELS = {
    TXT2IMG: "С нуля",
    IMG2IMG: "Поправить фото",
    INPAINT: "Удалить / заменить объект",
    OUTPAINT: "Расширить кадр",
}


class ValidationError(ValueError):
    """Запрос не принят; текст показывается пользователю."""


@dataclass(frozen=True)
class Padding:
    left: int = 0
    top: int = 0
    right: int = 0
    bottom: int = 0

    def any(self) -> bool:
        return any((self.left, self.top, self.right, self.bottom))


@dataclass(frozen=True)
class GenerationSpec:
    mode: str
    model: str
    prompt: str
    style: str
    width: int  # размер результата; для outpaint — холст вместе с полями
    height: int
    detail: str
    steps: int
    preset: str | None  # пресет сэмплера вместо шагов (Ideogram 4)
    guidance: float | None
    negative_prompt: str
    strength: float | None  # сила изменений: 1.0 — перерисовать полностью
    seed: int  # -1 — выбрать случайный при постановке в очередь
    source: str | None = None  # id загруженного исходника
    mask: str | None = None
    padding: Padding = Padding()

    def full_prompt(self) -> str:
        suffix = STYLES[self.style][1]
        return f"{self.prompt}, {suffix}" if suffix else self.prompt


def _integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float) or value != int(value):
        raise ValidationError(f"{name}: нужно целое число.")
    return int(value)


def _number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValidationError(f"{name}: нужно число.")
    return float(value)


def _choose_model(payload: dict, mode: str, available: list[Available]) -> Available:
    model_id = payload.get("model")
    if model_id in (None, ""):
        chosen = models.default(available, mode)
        if chosen is None:
            raise ValidationError(
                f"Режим «{MODE_LABELS[mode]}» не умеет ни одна включённая модель — включи её во вкладке «Настройки»."
            )
    else:
        chosen = next((m for m in available if m.id == model_id), None)
        if chosen is None:
            raise ValidationError(models.unknown(model_id))
    if mode not in chosen.model.modes:
        raise ValidationError(f"{chosen.label} не умеет режим «{MODE_LABELS[mode]}». Выбери другую модель.")
    if not chosen.installed:
        raise ValidationError(f"Модель {chosen.label} не установлена: запусти make models M={chosen.id}.")
    return chosen


def _source_size(payload: dict, size_of: Callable[[str], tuple[int, int] | None]) -> tuple[str, int, int]:
    source = payload.get("source")
    if not isinstance(source, str) or not source:
        raise ValidationError("Загрузи исходное фото.")
    size = size_of(source)
    if size is None:
        raise ValidationError("Исходное фото не найдено или это не PNG — загрузи его заново.")
    width, height = size
    if width % GRID or height % GRID or not (MIN_SIDE <= width <= MAX_SIDE and MIN_SIDE <= height <= MAX_SIDE):
        raise ValidationError(
            f"Исходник {width}×{height}: стороны должны быть кратны {GRID} и лежать в пределах "
            f"{MIN_SIDE}–{MAX_SIDE} px. Интерфейс готовит фото сам — загрузи его через страницу."
        )
    return source, width, height


# Подписи сторон как на странице (static/photo/app.js PAD_LABELS): в ошибках — они, а не ключи JSON.
PAD_LABELS = {"left": "Слева", "top": "Сверху", "right": "Справа", "bottom": "Снизу"}


def _padding(raw: object, width: int, height: int) -> Padding:
    if not isinstance(raw, dict):
        raise ValidationError("Укажи, в какие стороны расширять фото.")
    sides = {}
    for side in ("left", "top", "right", "bottom"):
        value = _integer(raw.get(side, 0), f"Поле «{PAD_LABELS[side]}»")
        if value < 0 or value % GRID:
            raise ValidationError(f"Поле «{PAD_LABELS[side]}»: нужно число от 0, кратное {GRID}.")
        sides[side] = value
    padding = Padding(**sides)
    if not padding.any():
        raise ValidationError("Расширение не задано: добавь поле хотя бы с одной стороны.")
    if width + padding.left + padding.right > MAX_SIDE or height + padding.top + padding.bottom > MAX_SIDE:
        raise ValidationError(f"Холст с полями больше {MAX_SIDE} px по стороне — уменьши поля или рабочий размер.")
    return padding


def build_spec(
    payload: object,
    available: list[Available],
    size_of: Callable[[str], tuple[int, int] | None],
) -> GenerationSpec:
    """Запрос API → проверенная спецификация. size_of — размер загруженного PNG по id."""
    if not isinstance(payload, dict):
        raise ValidationError("Тело запроса должно быть JSON-объектом.")

    mode = payload.get("mode") or TXT2IMG
    if mode not in models.MODES:
        raise ValidationError("Неизвестный режим.")
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValidationError("Напиши промпт.")
    prompt = prompt.strip()
    if len(prompt) > MAX_PROMPT:
        raise ValidationError(f"Промпт длиннее {MAX_PROMPT} символов.")

    chosen = _choose_model(payload, mode, available)
    model = chosen.model

    style = payload.get("style") or DEFAULT_STYLE
    if style not in STYLES:
        raise ValidationError("Неизвестный стиль.")
    detail = payload.get("detail") or DEFAULT_DETAIL
    if detail not in models.DETAILS:
        raise ValidationError("Неизвестный уровень детализации.")
    level = models.DETAILS.index(detail)
    steps, preset = model.steps[level], None
    if model.presets:
        preset = model.presets[level]
        if payload.get("steps") is not None:
            raise ValidationError(f"У {model.label} число шагов задаёт уровень детализации.")
    elif payload.get("steps") is not None:
        steps = _integer(payload["steps"], "Шаги")
        if not 1 <= steps <= model.max_steps:
            raise ValidationError(f"Шаги: от 1 до {model.max_steps} для {model.label}.")

    guidance = model.guidance[0] if model.guidance else model.fixed_guidance
    if payload.get("guidance") is not None:
        if not model.guidance:
            raise ValidationError(f"{model.label} не слушает guidance.")
        guidance = _number(payload["guidance"], "Guidance")
        low, high = model.guidance[1:]
        if not low <= guidance <= high:
            raise ValidationError(f"Guidance: от {low:g} до {high:g}.")

    negative = payload.get("negative_prompt") or ""
    if not isinstance(negative, str) or len(negative) > MAX_PROMPT:
        raise ValidationError("Негативный промпт: нужен текст не длиннее промпта.")
    negative = negative.strip()
    if negative and not model.negative:
        raise ValidationError(f"{model.label} не поддерживает негативный промпт.")

    strength = None
    if mode in model.strength_modes:
        strength = DEFAULT_STRENGTH[mode]
        if payload.get("strength") is not None:
            strength = _number(payload["strength"], "Сила изменений")
            if not MIN_STRENGTH <= strength <= 1.0:
                raise ValidationError(f"Сила изменений: от {MIN_STRENGTH:g} до 1.")
    elif payload.get("strength") is not None:
        raise ValidationError(f"У {model.label} в этом режиме нет настройки силы изменений.")

    seed = payload.get("seed")
    seed = -1 if seed in (None, "") else _integer(seed, "Seed")
    if not -1 <= seed <= MAX_SEED:
        raise ValidationError(f"Seed: от 0 до {MAX_SEED}.")

    source = mask = None
    padding = Padding()
    if mode == TXT2IMG:
        size = payload.get("size") or DEFAULT_SIZE
        if size not in SIZES:
            raise ValidationError("Неизвестный размер.")
        width, height, _ = SIZES[size]
    else:
        source, width, height = _source_size(payload, size_of)
    if mode == INPAINT:
        mask = payload.get("mask")
        if not isinstance(mask, str) or not mask:
            raise ValidationError("Выдели область, которую нужно изменить.")
        if size_of(mask) != (width, height):
            raise ValidationError("Маска не совпадает по размеру с фото — выдели область заново.")
    if mode == OUTPAINT:
        padding = _padding(payload.get("padding"), width, height)
        width += padding.left + padding.right
        height += padding.top + padding.bottom

    return GenerationSpec(
        mode=mode, model=model.id, prompt=prompt, style=style, width=width, height=height,
        detail=detail, steps=steps, preset=preset, guidance=guidance, negative_prompt=negative,
        strength=strength, seed=seed, source=source, mask=mask, padding=padding,
    )  # fmt: skip
