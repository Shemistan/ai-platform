"""Сколько займёт выпуск и что помешает ему дойти до канала — до «Запустить всё».

Ставки — по истории этой машины: медиана завершённых задач вкладок (картинка
по модели, увеличение, ролик по модели, секунды озвучки на символ по движку)
и прошлых сборок видео (доля от длины озвучки). Нет истории — значения по
умолчанию, близкие к Apple Silicon с моделями по умолчанию. Это ориентир, а
не обещание.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from aiplatform.studio import slides
from aiplatform.studio.steps import MAX_CLIPS, SHORT_WORDS

WORDS_PER_MIN = 130  # как в логе шага «Текст»
LONG_VOICE_S = 12 * 60  # дольше — предлагаем серию или выжимку
IMAGE_SHARE = 0.5  # до плана: примерно половина слайдов — картинки
DEFAULTS = {
    "image": 60.0,
    "upscale-2k": 20.0,
    "upscale-4k": 90.0,
    "clip": 300.0,
    "tts_per_char": 0.08,  # Higgs на M-серии: ~2800 с на 33 тыс. знаков
    "render_ratio": 0.4,  # сборка 533 с видео шла ~190 с
    "llm": 60.0,
    "collect": 600.0,  # сбор новостей, агент и судья narrator
}
MIN_SAMPLES = 1


def _median(values: list[float]) -> float | None:
    values = [v for v in values if v and v > 0]
    return statistics.median(values) if len(values) >= MIN_SAMPLES else None


def _elapsed(job: dict) -> float | None:
    if job.get("status") != "done" or job.get("started_at") is None or job.get("finished_at") is None:
        return None
    return job["finished_at"] - job["started_at"]


@dataclass
class Rates:
    """Секунды на единицу работы; from_history — сколько задач легло в основу."""

    image: dict[str, float] = field(default_factory=dict)  # по модели
    upscale: dict[str, float] = field(default_factory=dict)  # 2k / 4k
    clip: dict[str, float] = field(default_factory=dict)  # по модели
    tts_per_char: dict[str, float] = field(default_factory=dict)  # по движку
    render_ratio: float | None = None
    from_history: int = 0

    def image_s(self, model: str | None) -> float:
        return self.image.get(model or "") or _median(list(self.image.values())) or DEFAULTS["image"]

    def upscale_s(self, target: str) -> float:
        return self.upscale.get(target) or DEFAULTS.get(f"upscale-{target}", 0.0)

    def clip_s(self, model: str | None) -> float:
        return self.clip.get(model or "") or _median(list(self.clip.values())) or DEFAULTS["clip"]

    def tts_s(self, engine: str | None, chars: int) -> float:
        rate = self.tts_per_char.get(engine or "") or DEFAULTS["tts_per_char"]
        return rate * chars

    def render_s(self, audio_s: float) -> float:
        return (self.render_ratio or DEFAULTS["render_ratio"]) * audio_s


def rates(photo_jobs: list[dict], video_jobs: list[dict], audio_jobs: list[dict], projects: list[dict]) -> Rates:
    out = Rates()
    by: dict[str, list[float]] = {}
    for job in photo_jobs:
        seconds = _elapsed(job)
        if seconds is None:
            continue
        if job.get("mode") == "upscale":
            target = "4k" if max(job.get("width") or 0, job.get("height") or 0) >= 3000 else "2k"
            by.setdefault(f"upscale:{target}", []).append(seconds)
        elif job.get("mode") == "txt2img":
            by.setdefault(f"image:{job.get('model')}", []).append(seconds)
    for job in video_jobs:
        seconds = _elapsed(job)
        if seconds is not None:
            by.setdefault(f"clip:{job.get('model')}", []).append(seconds)
    for job in audio_jobs:
        seconds = _elapsed(job)
        if seconds is not None and job.get("chars"):
            by.setdefault(f"tts:{job.get('engine')}", []).append(seconds / job["chars"])
    ratios = []
    for project in projects:
        state = project.get("steps", {}).get("render", {})
        if state.get("status") == "done" and state.get("started_at") is not None and state.get("media_s"):
            ratios.append((state["updated_at"] - state["started_at"]) / state["media_s"])
    for name, values in by.items():
        kind, _, what = name.partition(":")
        target = {"image": out.image, "upscale": out.upscale, "clip": out.clip, "tts": out.tts_per_char}[kind]
        target[what] = _median(values)
        out.from_history += len(values)
    out.render_ratio = _median(ratios)
    out.from_history += len(ratios)
    return out


def voice_seconds(words: int, tempo: float = 1.0) -> float:
    return words / WORDS_PER_MIN * 60 / (tempo or 1.0)


def estimate(
    *,
    scenario: str,
    settings: dict,
    text: str,
    plan: list[slides.Slide] | None,
    statuses: dict[str, str],
    have_images: int,
    rates: Rates,
    audio_s: float | None = None,
) -> dict:
    """Оценка оставшегося: части по шагам, которые ещё не готовы, и итог в секундах."""
    todo = {s for s, status in statuses.items() if status not in ("done", "skipped")}
    parts: list[dict] = []

    def add(step: str, label: str, seconds: float, count: int | None = None) -> None:
        if step in todo and seconds > 0:
            parts.append({"step": step, "label": label, "seconds": round(seconds), "count": count})

    words = len(text.split())
    if scenario == "digest" and not text.strip():
        add("text", "Сбор новостей, пересказ и судья", DEFAULTS["collect"])
        words = 900  # обычный дайджест — до сбора текста нет
    upscale = settings.get("upscale_images") or "none"
    per_image = rates.image_s(settings.get("image_model")) + (rates.upscale_s(upscale) if upscale != "none" else 0)
    if scenario != "post":
        add("poster_prompt", "Промпт постера (LLM)", DEFAULTS["llm"])
    if settings.get("poster_mode") == "local":
        add("poster", "Постер", per_image, 1)
    if scenario != "post":
        add("plan", "План слайдов (LLM)", DEFAULTS["llm"] * 2)
        if settings.get("draw_images") and settings.get("images_mode") == "local" and not settings.get("draft"):
            if plan:
                images = sum(1 for s in plan if s.image_prompt)
            else:
                images = round(slides.target_slides(slides.paragraphs(text) or [" ".join(["x"] * words)]) * IMAGE_SHARE)
            images = max(images - have_images, 0)
            add("images", "Картинки слайдов", images * per_image, images)
        if settings.get("clips") and settings.get("clips_mode") == "local" and not settings.get("draft"):
            add("clips", "Ролики", MAX_CLIPS * rates.clip_s(settings.get("video_model")), MAX_CLIPS)
    chars = len(text) or words * 7
    if settings.get("voice_mode") == "tts":
        add("voice", "Озвучка", rates.tts_s(settings.get("engine"), chars))
    tempo = float(settings.get("tempo") or 1.0)
    length = (audio_s / tempo) if audio_s else voice_seconds(words, tempo)
    scale = 0.5 if settings.get("draft") else 1.0
    add("render", "Сборка видео", rates.render_s(length) * scale)
    if settings.get("shorts") and scenario != "post":
        short_s = voice_seconds(SHORT_WORDS, tempo)
        short_voice = rates.tts_s(settings.get("engine"), SHORT_WORDS * 7)
        add("short", "Шортс 9:16", DEFAULTS["llm"] + short_voice + rates.render_s(short_s))
    return {
        "parts": parts,
        "total_s": sum(p["seconds"] for p in parts),
        "video_s": round(length),
        "words": words,
        "from_history": rates.from_history,
    }
