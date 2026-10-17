"""Шаги сценариев «Дайджест новостей», «Видео по статье» и «Пост в канал».

У поста нет слайдов: видео — свой постер на всю длину своей озвучки (или
озвучки текста), подпись — post.md.

Файлы проекта:
  text.md            — текст для озвучки (пересказ новостей или статья)
  post.md            — подпись к посту
  poster_prompt.md   — промпт постера (первого кадра)
  poster.png|jpg|webp — постер: сгенерирован локально или загружен
  narrator/image_prompt.md, narrator/cover.* — промпт и картинка обложки из выпуска narrator (дайджест)
  plan.json          — {title, post, slides: [{para, sent, kind, title, bullets, image_prompt, …}]} (см. slides.py)
  images/NN.png|jpg|webp — картинки слайдов-картинок (+ NN.json — каким промптом, uploaded — загрузил человек)
  clips/NN.mp4|mov|webm  — видео-фоны слайдов: из вкладки «Видео» или от человека (+ NN.json)
  voice.mp3|wav|m4a  — озвучка
  video.mp4          — 16:9
  video-cover.jpg, video-thumb.jpg — первый кадр (обложка для Telegram) и миниатюра, если есть оформление
  short.json, short/voice.mp3, short.mp4 — шортс 9:16
  published.json     — журнал публикации: по каналу (и части поста) sending/sent/unknown/failed

Если в тексте упомянута организация из реестра запрещённых в РФ
(updates/registry.py), на её слайдах внизу идёт мелкая пометка, а в подпись
к посту она дописывается в конце. Выключается настройкой disclaimers.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from aiplatform.core import log, media
from aiplatform.core.runner import Canceled
from aiplatform.studio import cache, news, prompts, publish, render, slides
from aiplatform.studio.config import StudioConfig
from aiplatform.studio.llm import StudioError, complete, extract_json, plain
from aiplatform.studio.pipeline import Skip, StepContext, Waiting
from aiplatform.studio.render import LANDSCAPE, VERTICAL, Branding, Renderer, intro_seconds
from aiplatform.updates import registry

TEXT, POST, POSTER_PROMPT, PLAN, SHORT = "text.md", "post.md", "poster_prompt.md", "plan.json", "short.json"
VIDEO, SHORT_VIDEO, PUBLISHED = "video.mp4", "short.mp4", "published.json"
IMAGE_EXT = (".png", ".jpg", ".webp")
AUDIO_EXT = (".mp3", ".wav", ".m4a")
CLIP_EXT = (".mp4", ".mov", ".webm")
IMAGES, CLIPS = "images", "clips"
# Что лежит в каталоге слайдов: расширения и что показать в сообщениях.
MEDIA = {IMAGES: (IMAGE_EXT, "картинки"), CLIPS: (CLIP_EXT, "видео")}
IMAGE_SIZE = "1344x768"  # 16:9 из размеров вкладки «Фото»
DIGEST_POSTER_SIZE = "1024x1024"  # обложка дайджеста квадратная, как у narrator: в кадре — целиком
FRAMES = ("landscape", "square", "poster")  # кадр видео: 16:9, 1:1 или в пропорциях постера
SQUARE_IMAGE_SIZE = "1024x1024"  # картинки слайдов под кадр, который не 16:9
NARRATOR_PROMPT = "narrator/image_prompt.md"
NARRATOR_COVER = "narrator/cover"
NARRATOR_FOLDER = "narrator/folder.txt"  # каталог выпуска: туда же человек кладёт обложку из ChatGPT
NARRATOR_REVIEW = "narrator/review.md"  # отчёт судьи выпуска: вердикт и что исправлено
VERDICTS = ("готово", "готово с оговорками", "не готово")
NOT_READY = "не готово"
_VERDICT = re.compile(r"^\s*\**\s*Вердикт\s*\**\s*:\s*\**\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)
NARRATOR_COVER_EXT = (".png", ".jpg", ".jpeg", ".webp")
CLIP_PRESET = "landscape-m"  # 704×448 из пресетов вкладки «Видео»: быстрее, а ffmpeg растянет до кадра
CLIP_DURATION_S = 5  # есть у всех видеомоделей; длиннее слайд — ролик идёт по кругу
MAX_CLIPS = 3  # роликов на выпуск: ключевые моменты, остальные слайды — картинка с наездом
UPSCALE_TARGETS = ("2k", "4k")
TEMPOS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)  # скорость озвучки в видео, как DOWNLOAD_TEMPOS у narrator
SHORT_WORDS = 140
DRAFT_SCALE = 0.5  # черновик — половина кадра
POLL_S = 0.3


@dataclass
class Services:
    config: StudioConfig
    photo: object  # photo.jobs.JobManager: create / wait / image_path
    audio: object  # audio.jobs.AudioJobs: create / wait / audio_path
    video: object  # video.jobs.JobManager: create / get / cancel / video_path
    renderer: Renderer
    cache: cache.MediaCache  # готовые картинки, ролики и озвучка по входам генерации


def services(ctx: StepContext) -> Services:
    return ctx.studio.services


def find(folder: Path, stem: str, exts: tuple[str, ...]) -> Path | None:
    found = [folder / f"{stem}{ext}" for ext in exts if (folder / f"{stem}{ext}").is_file()]
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def require_text(ctx: StepContext) -> str:
    text = ctx.read(TEXT).strip()
    if not text:
        raise StudioError("Нет текста — заполни шаг «Текст».")
    return text


def llm(ctx: StepContext, prompt: str) -> str:
    def on_line(line: str) -> None:
        if line.strip():
            ctx.progress("LLM пишет…")

    ctx.progress("LLM думает…")
    return complete(prompt, cmd=services(ctx).config.llm_cmd, cancel=ctx.cancel, on_line=on_line)


def wait_job(ctx: StepContext, tab: str, jobs: object, job: dict, what: str) -> dict:
    """Задача вкладки «Фото» или «Аудио»: запомнить её id, ждать с прогрессом, вернуть итог."""
    ctx.remember(job={"tab": tab, "id": job["id"]})
    ctx.log(f"{what}: задача вкладки «{tab}» {job['id']}")
    while True:
        current = jobs.get(job["id"])
        if current is None:
            raise StudioError(f"{what}: задача вкладки пропала.")
        progress = current.get("progress") or {}
        ctx.progress(f"{what}: {progress.get('stage') or ''}", progress.get("percent"))
        if current["status"] not in ("queued", "running"):
            break
        if ctx.cancel.wait(POLL_S):
            jobs.cancel(job["id"])
            raise Canceled
    if current["status"] == "canceled":
        raise StudioError(f"{what}: задачу отменили на вкладке — перезапусти шаг.")
    if current["status"] != "done":
        raise StudioError(f"{what}: {current.get('error') or 'задача не удалась'}")
    return current


def from_cache(ctx: StepContext, entry: str, target: Path, what: str) -> bool:
    """Готовый файл с теми же входами — из кэша. «Перезапустить шаг» кэш не читает: просят новый вариант."""
    if ctx.state.get("fresh") or not services(ctx).cache.take(entry, target):
        return False
    ctx.log(f"{what}: из кэша")
    return True


def generate_image(ctx: StepContext, prompt: str, target: Path, what: str, size: str = IMAGE_SIZE) -> str | None:
    """Картинка во вкладке «Фото» (и увеличение, если включено). Возвращает id исходной задачи;
    None — картинка из кэша, задачи не было."""
    svc = services(ctx)
    model, upscale = ctx.settings["image_model"], ctx.settings.get("upscale_images") or "none"
    entry = cache.entry_id("image", prompt=prompt, model=model, size=size, upscale=upscale)
    if from_cache(ctx, entry, target, what):
        return None
    payload = {"mode": "txt2img", "prompt": prompt, "size": size, "model": model}
    try:
        job = svc.photo.create(payload)
    except ValueError as exc:
        raise StudioError(f"{what}: {exc}") from exc
    done = wait_job(ctx, "photo", svc.photo, job, what)
    finish_image(ctx, done["id"], target, what)
    svc.cache.put(entry, target)
    return done["id"]


def finish_image(ctx: StepContext, job_id: str, target: Path, what: str) -> None:
    """Положить картинку задачи «Фото» в проект; с настройкой «увеличить» — через апскейл вкладки."""
    svc = services(ctx)
    upscale = ctx.settings.get("upscale_images")
    if upscale in UPSCALE_TARGETS:
        try:
            job = svc.photo.upscale(job_id, {"target": upscale})
        except ValueError as exc:
            raise StudioError(f"{what}: увеличить до {upscale.upper()} не вышло: {exc}") from exc
        if job is None:
            raise StudioError(f"{what}: исходная задача «Фото» пропала — перезапусти шаг.")
        job_id = wait_job(ctx, "photo", svc.photo, job, f"{what}, увеличение до {upscale.upper()}")["id"]
    source = svc.photo.image_path(job_id)
    if source is None:
        raise StudioError(f"{what}: картинка не найдена.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)


def generate_clip(ctx: StepContext, prompt: str, target: Path, what: str) -> Path:
    svc = services(ctx)
    payload = {
        "prompt": prompt,
        "duration_s": CLIP_DURATION_S,
        "preset": CLIP_PRESET,
        "model": ctx.settings["video_model"],
        "audio": False,  # звук ролика всё равно не идёт в видео: под слайдом — озвучка
    }
    entry = cache.entry_id("clip", **{k: payload[k] for k in ("prompt", "duration_s", "preset", "model")})
    if from_cache(ctx, entry, target, what):
        return target
    try:
        job = svc.video.create(payload)
    except ValueError as exc:
        raise StudioError(f"{what}: {exc}") from exc
    done = wait_job(ctx, "video", svc.video, job, what)
    source = svc.video.video_path(done["id"])
    if source is None:
        raise StudioError(f"{what}: mp4 не найден.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    svc.cache.put(entry, target)
    return target


def speak(ctx: StepContext, text: str, target: Path, what: str) -> Path:
    svc = services(ctx)
    settings = ctx.settings
    payload = {"text": text, "engine": settings["engine"], "voice": settings.get("voice") or None}
    if settings.get("speed"):
        payload["speed"] = settings["speed"]
    entry = cache.entry_id("voice", **payload)
    if from_cache(ctx, entry, target, what):
        return target
    try:
        job = svc.audio.create(payload)
    except ValueError as exc:
        raise StudioError(f"{what}: {exc}") from exc
    done = wait_job(ctx, "audio", svc.audio, job, what)
    source = svc.audio.audio_path(done["id"])
    if source is None:
        raise StudioError(f"{what}: mp3 не найден.")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    svc.cache.put(entry, target)
    return target


def media_prompts(kind: str, plan: list[slides.Slide]) -> list[tuple[int, str]]:
    """(номер слайда, промпт) для слайдов, которым нужна картинка или ролик. Роликов — не больше MAX_CLIPS."""
    if kind == IMAGES:
        return [(n, s.image_prompt) for n, s in enumerate(plan, start=1) if s.image_prompt]
    return [(n, s.video_prompt) for n, s in enumerate(plan, start=1) if s.video_prompt][:MAX_CLIPS]


def read_meta(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def slide_media(folder: Path, n: int, kind: str, prompt: str) -> Path | None:
    """Файл слайда n, если он сделан по текущему промпту (после смены промпта старый не годится)."""
    path = find(folder, f"{n:02d}", MEDIA[kind][0])
    if path is None or read_meta(folder / f"{n:02d}.json").get("prompt") != prompt:
        return None
    return path


def save_slide_media(folder: Path, n: int, ext: str, data: bytes, prompt: str, kind: str) -> Path:
    """Файл слайда от человека: прежний вариант (любого формата) заменяется."""
    folder.mkdir(parents=True, exist_ok=True)
    for old in (folder / f"{n:02d}{e}" for e in MEDIA[kind][0]):
        old.unlink(missing_ok=True)
    path = folder / f"{n:02d}.{ext}"
    path.write_bytes(data)
    meta = json.dumps({"prompt": prompt, "uploaded": True}, ensure_ascii=False)
    (folder / f"{n:02d}.json").write_text(meta, encoding="utf-8")
    return path


def prune(folder: Path, keep: set[str]) -> None:
    """Убрать файлы слайдов, которых больше нет в плане."""
    for old in folder.glob("*"):
        if old.is_file() and old.stem not in keep:
            old.unlink()


def uploads_ready(ctx: StepContext, kind: str, items: list[tuple[int, str]]) -> None:
    """Ждать файлы от человека; «Продолжить» — собрать без недостающих."""
    folder = ctx.path(kind)
    missing = [n for n, prompt in items if slide_media(folder, n, kind, prompt) is None]
    what = MEDIA[kind][1]
    have = len(items) - len(missing)
    if missing and not ctx.state.get("approved"):
        rest = "картинка или градиент" if kind == CLIPS else "градиентный фон"
        raise Waiting(
            f"Сделай {what} по промптам во внешней модели и перетащи их в окна справа: готово {have} из "
            f"{len(items)}. «Продолжить» соберёт видео без недостающих — у них будет {rest}."
        )
    if missing:
        ctx.log(f"без {what}: слайды {', '.join(str(n) for n in missing)}")
    ctx.log(f"{what}: {have} из {len(items)}")


def banned(config: StudioConfig) -> list[registry.Entry]:
    return registry.Registry(config.registry_file).entries()


def slide_notes(paras: list[str], plan: list[slides.Slide], entries: list[registry.Entry]) -> list[list[str]]:
    """Пометки каждого слайда: об организациях из его абзацев, заголовка и тезисов."""
    out = []
    for n, slide in enumerate(plan):
        said = "\n".join([slides.spoken(paras, plan, n), slide.title, *slide.bullets, *scene_texts(slide)])
        out.append(registry.notes(said, entries))
    return out


def scene_texts(slide: slides.Slide) -> list[str]:
    """Тексты динамической сцены, которые видно на экране: подписи, шаги, сравнение, цитата."""
    data = slide.data
    out = [str(data.get(k) or "") for k in ("label", "text", "author")]
    out += data.get("steps") or []
    out += [str(p.get("label") or "") for p in data.get("data") or []]
    for side in (data.get("left"), data.get("right")):
        if side:
            out += [side.get("title") or "", *side.get("items", [])]
    return [t for t in out if t]


def load_plan(ctx: StepContext) -> tuple[dict, list[slides.Slide]]:
    raw = ctx.read(PLAN)
    if not raw:
        raise StudioError("Нет плана слайдов — выполни шаг «Слайды».")
    data = json.loads(raw)
    paras = slides.paragraphs(require_text(ctx))
    return data, slides.parse_plan(data, paras)


# --- шаги ---


POST_SCENARIO = "post"


def is_post(ctx: StepContext) -> bool:
    return ctx.project["scenario"] == POST_SCENARIO


def no_slides(ctx: StepContext) -> None:
    if is_post(ctx):
        raise Skip("В посте нет слайдов: видео — постер под звук.")


def step_text(ctx: StepContext) -> None:
    if is_post(ctx):
        post = ctx.read(POST).strip()
        if not post:
            raise StudioError("Нет подписи к посту — заполни её.")
        if not ctx.read(TEXT).strip():  # озвучить нечего своего — озвучиваем подпись
            ctx.write(TEXT, publish.plain_text(post) + "\n")
        ctx.log(f"подпись: {len(publish.plain_text(post))} знаков из {publish.CAPTION_LIMIT}")
        if ctx.settings.get("review_text") and not ctx.state.get("approved"):
            raise Waiting("Проверь подпись и текст, затем нажми «Продолжить».")
        return
    if ctx.project["scenario"] == "digest":
        day = ctx.project["day"]
        ctx.progress(f"narrator собирает новости за {day}…")
        folder = news.collect(day, services(ctx).config, ctx.cancel, lambda line: line.strip() and ctx.log(line))
        ctx.write(TEXT, (folder / news.NARRATION).read_text(encoding="utf-8"))
        post = folder / news.POST
        if post.is_file():
            ctx.write(POST, post.read_text(encoding="utf-8"))
        take_narrator_cover(ctx, folder)
        take_review(ctx, folder)
        ctx.log(f"выпуск: {folder}")
    text = require_text(ctx)
    words = len(text.split())
    ctx.log(f"{words} слов, {len(slides.paragraphs(text))} абзацев, ~{max(round(words / 130), 1)} мин озвучки")
    if ctx.settings.get("review_text") and not ctx.state.get("approved"):
        raise Waiting("Проверь и при необходимости поправь текст, затем нажми «Продолжить».")


def verdict(text: str) -> str | None:
    """Вердикт судьи из строки «Вердикт: …», как review_verdict у движка; None — нет строки."""
    m = _VERDICT.search(text or "")
    if not m:
        return None
    value = m[1].strip(" .*—-").lower()
    return value if value in VERDICTS else None


def take_review(ctx: StepContext, folder: Path) -> None:
    """review.md судьи — в проект: вердикт виден в шаге «Текст» и держит публикацию."""
    review = folder / news.REVIEW
    if not review.is_file():
        ctx.log("судья выпуск не смотрел: review.md нет")
        return
    text = review.read_text(encoding="utf-8")
    ctx.write(NARRATOR_REVIEW, text)
    ctx.log(f"судья: {verdict(text) or 'нет строки «Вердикт: …»'}")


def take_narrator_cover(ctx: StepContext, folder: Path) -> None:
    """Промпт обложки (image_prompt.md) и готовую картинку (cover.*) выпуска narrator — в проект."""
    for old in ctx.path("narrator").glob("*") if ctx.path("narrator").is_dir() else []:
        old.unlink()
    source = folder / news.IMAGE_PROMPT
    if source.is_file() and source.read_text(encoding="utf-8").strip():
        ctx.write(NARRATOR_PROMPT, source.read_text(encoding="utf-8").strip() + "\n")
        ctx.log("промпт обложки — из narrator")
    ctx.write(NARRATOR_FOLDER, str(folder) + "\n")
    if pull_narrator_cover(ctx):
        ctx.log("обложка выпуска — из narrator")


def pull_narrator_cover(ctx: StepContext) -> bool:
    """Свежая cover.* из каталога выпуска narrator — в проект. True — обложка появилась или сменилась."""
    folder = Path(ctx.read(NARRATOR_FOLDER).strip() or ".")
    cover = find(folder, "cover", NARRATOR_COVER_EXT) if ctx.read(NARRATOR_FOLDER).strip() else None
    if cover is None:
        return False
    current = narrator_cover(ctx)
    if current is not None and current.read_bytes() == cover.read_bytes():
        return False
    if current is not None:
        current.unlink()
    shutil.copyfile(cover, ctx.path(NARRATOR_COVER + cover.suffix.lower()))
    return True


def narrator_cover(ctx: StepContext) -> Path | None:
    return find(ctx.path("narrator"), "cover", NARRATOR_COVER_EXT)


def use_narrator_cover(ctx: StepContext, prompt: str) -> bool:
    """Постер — готовая обложка выпуска narrator, если она есть."""
    ready = narrator_cover(ctx)
    if ready is None:
        return False
    for old in (ctx.dir / f"poster{ext}" for ext in IMAGE_EXT):
        old.unlink(missing_ok=True)
    ext = ".jpg" if ready.suffix == ".jpeg" else ready.suffix
    shutil.copyfile(ready, ctx.path(f"poster{ext}"))
    ctx.path("poster.json").write_text(json.dumps({"prompt": prompt, "narrator": True}), encoding="utf-8")
    ctx.log("постер — готовая обложка выпуска narrator")
    return True


def step_poster_prompt(ctx: StepContext) -> None:
    if is_post(ctx) and ctx.settings["poster_mode"] == "upload":
        raise Skip("Постер свой — промпт не нужен.")
    text = require_text(ctx)
    if ctx.project["scenario"] == "digest":
        own = ctx.read(NARRATOR_PROMPT).strip()
        if own:  # как в narrator: сцена по событиям дня и заголовки на картинке
            ctx.write(POSTER_PROMPT, own + "\n")
            ctx.log("промпт обложки — из выпуска narrator")
            return
        prompt = plain(llm(ctx, prompts.digest_poster(text, ctx.project["day"])))
    else:
        prompt = plain(llm(ctx, prompts.poster(text, ctx.project["title"])))
    if not prompt:
        raise StudioError("LLM не придумала промпт постера.")
    ctx.write(POSTER_PROMPT, prompt + "\n")


def step_poster(ctx: StepContext) -> None:
    existing = find(ctx.dir, "poster", IMAGE_EXT)
    prompt = ctx.read(POSTER_PROMPT).strip()
    if ctx.settings["poster_mode"] == "upload":
        # Обложку делают в ChatGPT: её перетаскивают сюда или кладут в каталог выпуска narrator.
        from_narrator = existing is None or read_meta(ctx.path("poster.json")).get("narrator")
        if from_narrator and (pull_narrator_cover(ctx) or existing is None) and use_narrator_cover(ctx, prompt):
            return
        if existing is None and is_post(ctx):
            raise Waiting("Загрузи свой постер (PNG, JPEG или WebP): перетащи его сюда или нажми «Загрузить постер».")
        if existing is None:
            where = ""
            if ctx.read(NARRATOR_FOLDER).strip():
                where = f" или положи её в {ctx.read(NARRATOR_FOLDER).strip()} и нажми «Перезапустить шаг»"
            raise Waiting(f"Сгенерируй картинку по промпту (например, в ChatGPT) и загрузи её сюда{where}.")
        ctx.log(f"постер загружен: {existing.name}")
        return
    if not prompt:
        raise StudioError("Нет промпта постера.")
    if narrator_cover(ctx) is not None and prompt == ctx.read(NARRATOR_PROMPT).strip():
        use_narrator_cover(ctx, prompt)
        return
    model, upscale = ctx.settings["image_model"], ctx.settings.get("upscale_images") or "none"
    meta_path, target = ctx.path("poster.json"), ctx.path("poster.png")
    meta = read_meta(meta_path)
    job_id = meta.get("job")
    only_upscale = (
        meta.get("prompt") == prompt
        and meta.get("model") == model
        and meta.get("upscale", "none") != upscale
        and job_id
        and services(ctx).photo.image_path(job_id) is not None
        and existing == target
    )
    if only_upscale:
        finish_image(ctx, job_id, target, "Постер")  # тот же постер, другое увеличение
    else:
        ctx.progress("Ставлю в очередь «Фото»…")
        for old in (ctx.dir / f"poster{ext}" for ext in IMAGE_EXT):
            old.unlink(missing_ok=True)
        size = DIGEST_POSTER_SIZE if ctx.project["scenario"] == "digest" else IMAGE_SIZE
        job_id = generate_image(ctx, prompt, target, "Постер", size)
    want = {"prompt": prompt, "model": model, "job": job_id, "upscale": upscale}
    meta_path.write_text(json.dumps(want, ensure_ascii=False), encoding="utf-8")


def step_plan(ctx: StepContext) -> None:
    no_slides(ctx)
    text = require_text(ctx)
    paras = slides.paragraphs(text)
    aspect = "16:9" if ctx.settings.get("frame", "landscape") == "landscape" else "1:1"
    seconds = float(ctx.settings.get("slide_s") or slides.TARGET_SLIDE_S)
    target = slides.target_slides(paras, seconds)
    answer = llm(ctx, prompts.plan(paras, ctx.project["title"], target, aspect, seconds))
    data = extract_json(answer)
    plan = slides.parse_plan(data, paras)
    title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
    post = str(data.get("post") or "").strip() if isinstance(data, dict) else ""
    out = {"title": title or ctx.project["title"], "post": post, "slides": [s.to_dict() for s in plan]}
    ctx.write(PLAN, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    if post and not ctx.read(POST).strip():
        ctx.write(POST, post + "\n")
    if title and ctx.project.get("auto_title"):
        ctx.set_title(title)
    images = sum(s.kind == slides.IMAGE for s in plan)
    ctx.log(f"{len(plan)} слайдов: {images} с картинкой, {len(plan) - images} динамических")


def step_images(ctx: StepContext) -> None:
    no_slides(ctx)
    if not ctx.settings["draw_images"]:
        raise Skip("Картинки выключены: фон слайдов — градиент.")
    if ctx.settings.get("draft"):
        raise Skip("Черновик: без картинок — «Сделать чистовик» нарисует их.")
    _, plan = load_plan(ctx)
    todo = media_prompts(IMAGES, plan)
    folder = ctx.path(IMAGES)
    folder.mkdir(exist_ok=True)
    prune(folder, {f"{n:02d}" for n, _ in todo})
    if ctx.settings.get("images_mode") == "upload":
        uploads_ready(ctx, IMAGES, todo)
        return
    model = ctx.settings["image_model"]
    upscale = ctx.settings.get("upscale_images") or "none"
    size = slide_image_size(ctx)
    photo = services(ctx).photo
    for i, (n, prompt) in enumerate(todo, start=1):
        meta_path = folder / f"{n:02d}.json"
        meta = read_meta(meta_path)
        if meta.get("uploaded") and slide_media(folder, n, IMAGES, prompt):
            continue  # свою картинку человек положил сам — не перерисовываем
        target, what = folder / f"{n:02d}.png", f"Картинка слайда {n}"
        same = (
            meta.get("prompt") == prompt
            and meta.get("model") == model
            and meta.get("size", IMAGE_SIZE) == size
            and target.is_file()
        )
        if same and meta.get("upscale", "none") == upscale:
            continue
        ctx.progress(f"Картинка {i} из {len(todo)}", 100.0 * (i - 1) / len(todo))
        job_id = meta.get("job")
        if same and job_id and photo.image_path(job_id) is not None:
            finish_image(ctx, job_id, target, what)  # поменялось только увеличение — не перерисовываем
        else:
            for old in (folder / f"{n:02d}{ext}" for ext in IMAGE_EXT):
                old.unlink(missing_ok=True)
            job_id = generate_image(ctx, prompt, target, what, size)
        want = {"prompt": prompt, "model": model, "job": job_id, "upscale": upscale, "size": size}
        meta_path.write_text(json.dumps(want, ensure_ascii=False), encoding="utf-8")


def slide_image_size(ctx: StepContext) -> str:
    return IMAGE_SIZE if ctx.settings.get("frame", "landscape") == "landscape" else SQUARE_IMAGE_SIZE


def video_frame(ctx: StepContext, poster: Path | None) -> str:
    """Кадр видео 16:9 из настройки frame: широкий, квадрат или в пропорциях постера (нет постера — квадрат)."""
    choice = ctx.settings.get("frame", "landscape")
    if choice == "landscape":
        return LANDSCAPE
    if choice == "poster" and poster is not None:
        return render.frame(*media.video_size(poster, services(ctx).config.ffmpeg))
    return render.frame(1, 1)


def step_clips(ctx: StepContext) -> None:
    no_slides(ctx)
    if not ctx.settings.get("clips"):
        raise Skip("Ролики выключены: фон слайдов — картинки с медленным наездом.")
    if ctx.settings.get("draft"):
        raise Skip("Черновик: без роликов — «Сделать чистовик» сделает их.")
    _, plan = load_plan(ctx)
    todo = media_prompts(CLIPS, plan)
    folder = ctx.path(CLIPS)
    folder.mkdir(exist_ok=True)
    prune(folder, {f"{n:02d}" for n, _ in todo})
    if ctx.settings.get("clips_mode") == "upload":
        uploads_ready(ctx, CLIPS, todo)
        return
    model = ctx.settings["video_model"]
    for i, (n, prompt) in enumerate(todo, start=1):
        meta = read_meta(folder / f"{n:02d}.json")
        if meta.get("uploaded") and slide_media(folder, n, CLIPS, prompt):
            continue  # свой ролик человек положил сам — не перегенерируем
        want = {"prompt": prompt, "model": model}
        target = folder / f"{n:02d}.mp4"
        if meta == want and target.is_file():
            continue
        ctx.progress(f"Ролик {i} из {len(todo)}", 100.0 * (i - 1) / len(todo))
        for old in (folder / f"{n:02d}{ext}" for ext in CLIP_EXT):
            old.unlink(missing_ok=True)
        generate_clip(ctx, prompt, target, f"Ролик слайда {n}")
        meta_text = json.dumps(want, ensure_ascii=False)
        (folder / f"{n:02d}.json").write_text(meta_text, encoding="utf-8")


def step_voice(ctx: StepContext) -> None:
    existing = find(ctx.dir, "voice", AUDIO_EXT)
    if ctx.settings["voice_mode"] == "upload":
        if existing is None:
            raise Waiting("Загрузи озвучку (mp3, wav или m4a) — например, из Gemini TTS.")
        ctx.log(f"озвучка загружена: {existing.name}")
        return
    text = require_text(ctx)
    for old in (ctx.dir / f"voice{ext}" for ext in AUDIO_EXT):
        old.unlink(missing_ok=True)
    speak(ctx, text, ctx.path("voice.mp3"), "Озвучка")


def _media_files(folder: Path, exts: tuple[str, ...]) -> list[Path]:
    return sorted(p for p in folder.glob("*") if p.suffix in exts) if folder.is_dir() else []


def scenes(
    folder: Path,
    plan: list[slides.Slide],
    durations: list[float],
    cycle: bool = False,
    *,
    images: bool = True,
    clips: bool = False,
    notes: list[list[str]] | None = None,
) -> list[dict]:
    """Сцены слайдов. Фон: видео (если включены), иначе картинка (если включены), иначе градиент.
    cycle — у шортса свой план: файлы слайдов идут по кругу. notes — пометки слайдов."""
    enabled = {IMAGES: images, CLIPS: clips}
    found = {kind: _media_files(folder / kind, MEDIA[kind][0]) if enabled[kind] else [] for kind in MEDIA}

    def pick(kind: str, n: int) -> Path | None:
        own = find(folder / kind, f"{n:02d}", MEDIA[kind][0]) if found[kind] else None
        if own is None and cycle and found[kind]:
            own = found[kind][(n - 1) % len(found[kind])]
        return own

    # Есть главы — сверху ролика полоса прогресса с названием текущей главы.
    names = slides.chapters(plan)
    total = sum(durations)
    begin = 0.0
    out = []
    for n, (slide, seconds) in enumerate(zip(plan, durations, strict=True), start=1):
        progress = render.Progress(begin, begin + seconds, total, names[n - 1]) if any(names) else None
        begin += seconds
        out.append(
            {
                "title": slide.title,
                "bullets": slide.bullets,
                "image": pick(IMAGES, n),
                "clip": pick(CLIPS, n),
                "notes": notes[n - 1] if notes else [],
                "seconds": seconds,
                "kind": slide.kind,
                "data": slide.data,
                "progress": progress,
            }
        )
    return out


def render_video(
    ctx: StepContext,
    *,
    text: str,
    plan: list[slides.Slide],
    audio: Path,
    out: Path,
    orientation: str,
    cycle: bool,
    source: str = "",
) -> None:
    """source — строка «Источник: …» для последнего слайда (видео по чужой статье)."""
    svc = services(ctx)
    audio = paced(ctx, audio)
    audio_s = media.duration(audio, svc.config.ffmpeg)
    if audio_s <= 0:
        raise StudioError(f"Не удалось узнать длительность {audio.name} — нужен ffprobe рядом с ffmpeg.")
    poster = find(ctx.dir, "poster", IMAGE_EXT)
    settings = ctx.settings
    brand = branding(svc.config) if settings.get("branding") else None
    intro = intro_seconds(brand) if poster is not None and audio_s > intro_seconds(brand) + 1 else 0.0
    paras = slides.paragraphs(text)
    # Время на чтение: слайд не сменится, пока не прочитан, — следующие догоняют озвучку позже.
    per_s = slides.READING.get(settings.get("reading", "normal"), 0.0)
    minimums = [slides.reading_s(slide, per_s) for slide in plan] if per_s else None
    durations = slides.durations(paras, plan, audio_s, intro, minimums)
    behind = slides.lag(paras, plan, durations, audio_s, intro)
    if behind >= 2:
        ctx.log(f"время на чтение: слайды отстают от озвучки до {behind:g} с — помогут слайды подлиннее в плане")
    notes = slide_notes(paras, plan, banned(svc.config)) if settings.get("disclaimers", True) else None
    marked = [n for n, found in enumerate(notes or [], start=1) if found]
    if marked:
        ctx.log(f"пометки о запрещённых организациях: слайды {', '.join(map(str, marked))}")
    shown = [n for n, d in enumerate(durations) if d > 0]
    if source and shown:  # источник — полосой внизу последнего слайда, как пометки
        notes = notes or [[] for _ in plan]
        notes[shown[-1]] = [*notes[shown[-1]], source]
        ctx.log(f"источник на последнем слайде: {source}")
    draft = bool(settings.get("draft"))
    scene_list = scenes(
        ctx.dir,
        plan,
        durations,
        cycle,
        images=settings["draw_images"] and not draft,
        clips=bool(settings.get("clips")) and not draft,
        notes=notes,
    )

    def on_progress(done: int, total: int) -> None:
        ctx.progress(f"Кадры: {done} из {total}", 90.0 * done / total)

    ctx.progress("Рисую кадры…", 0.0)
    renderer(ctx).build(
        poster=poster if intro else None,
        scenes=scene_list,
        audio=audio,
        audio_s=audio_s,
        out=out,
        orientation=orientation,
        cancel=ctx.cancel,
        on_progress=on_progress,
        branding=brand,
    )
    cover, thumb = covers(out)
    if brand is not None and intro:
        svc.renderer.cover_frames(out, cover, thumb, ctx.cancel)
    else:
        cover.unlink(missing_ok=True)
        thumb.unlink(missing_ok=True)
    size = "×".join(map(str, render.size_of(orientation)))
    ctx.log(f"{out.name}: {audio_s:.0f} с, {len(scene_list)} слайдов, кадр {size}")
    ctx.remember(media_s=round(audio_s, 1))  # для оценки времени следующих сборок


def renderer(ctx: StepContext) -> Renderer:
    """Черновик собирается в половинном кадре: быстро посмотреть структуру и ритм."""
    base = services(ctx).renderer
    return dataclasses.replace(base, scale=base.scale * DRAFT_SCALE) if ctx.settings.get("draft") else base


def paced(ctx: StepContext, audio: Path) -> Path:
    """Озвучка в темпе из настроек: слайды подстраиваются под её длину, обложка и плашка — в обычном темпе."""
    tempo = float(ctx.settings.get("tempo") or 1.0)
    if tempo == 1.0:
        return audio
    out = audio.with_name(f"{audio.stem}-x{tempo:g}.mp3")
    if not out.is_file() or out.stat().st_mtime < audio.stat().st_mtime:
        ctx.progress(f"Озвучка ×{tempo:g}…")
        services(ctx).renderer.retime(audio, out, tempo, ctx.cancel)
    ctx.log(f"скорость озвучки ×{tempo:g}")
    return out


def branding(config: StudioConfig) -> Branding:
    missing = [p for p in (config.sticker, config.play) if not p.is_file()]
    if missing:
        raise StudioError(f"Нет файла оформления {missing[0]} — проверь AIP_STUDIO_STICKER и AIP_STUDIO_PLAY.")
    return Branding(sticker=config.sticker, play=config.play)


def covers(video: Path) -> tuple[Path, Path]:
    """Обложка ролика для Telegram (первый кадр: плей и плашка) и её миниатюра."""
    return video.with_name(f"{video.stem}-cover.jpg"), video.with_name(f"{video.stem}-thumb.jpg")


def render_still(ctx: StepContext) -> None:
    """Пост: постер на всю длину озвучки, с оформлением — плей и плашка."""
    svc = services(ctx)
    poster = find(ctx.dir, "poster", IMAGE_EXT)
    if poster is None:
        raise StudioError("Нет постера — загрузи его в шаге «Постер».")
    audio = find(ctx.dir, "voice", AUDIO_EXT)
    if audio is None:
        raise StudioError("Нет озвучки — выполни шаг «Озвучка».")
    audio = paced(ctx, audio)
    audio_s = media.duration(audio, svc.config.ffmpeg)
    if audio_s <= 0:
        raise StudioError(f"Не удалось узнать длительность {audio.name} — нужен ffprobe рядом с ffmpeg.")
    brand = branding(svc.config) if ctx.settings.get("branding") else None
    out = ctx.path(VIDEO)
    ctx.progress("Собираю видео: постер под звук…", 10.0)
    orientation = video_frame(ctx, poster)
    notes = None
    if ctx.settings.get("disclaimers", True):  # видео могут переслать без подписи — пометка в кадре
        said = "\n".join([ctx.project["title"], ctx.read(TEXT), ctx.read(POST)])
        notes = registry.notes(said, banned(svc.config)) or None
        if notes:
            ctx.log(f"пометки о запрещённых организациях в кадре: {len(notes)}")
    renderer(ctx).still(
        poster=poster,
        audio=audio,
        audio_s=audio_s,
        out=out,
        orientation=orientation,
        cancel=ctx.cancel,
        branding=brand,
        notes=notes,
    )
    cover, thumb = covers(out)
    if brand is not None:
        svc.renderer.cover_frames(out, cover, thumb, ctx.cancel)
    else:
        cover.unlink(missing_ok=True)
        thumb.unlink(missing_ok=True)
    size = "×".join(map(str, render.size_of(orientation)))
    ctx.log(f"{out.name}: {audio_s:.0f} с, постер под звук, кадр {size}")


def step_render(ctx: StepContext) -> None:
    if is_post(ctx):
        render_still(ctx)
        return
    text = require_text(ctx)
    _, plan = load_plan(ctx)
    audio = find(ctx.dir, "voice", AUDIO_EXT)
    if audio is None:
        raise StudioError("Нет озвучки — выполни шаг «Озвучка».")
    orientation = video_frame(ctx, find(ctx.dir, "poster", IMAGE_EXT))
    source = source_line(ctx) if ctx.project["scenario"] == "article" else ""
    render_video(
        ctx,
        text=text,
        plan=plan,
        audio=audio,
        out=ctx.path(VIDEO),
        orientation=orientation,
        cycle=False,
        source=source,
    )


def parse_short(obj: object) -> list[dict]:
    beats = obj.get("beats") if isinstance(obj, dict) else obj
    if not isinstance(beats, list):
        raise StudioError("В сценарии шортса нет списка beats.")
    out = []
    for beat in beats:
        if isinstance(beat, dict) and str(beat.get("text") or "").strip():
            out.append(
                {"title": " ".join(str(beat.get("title") or "").split())[:60], "text": " ".join(beat["text"].split())}
            )
    if not out:
        raise StudioError("В сценарии шортса ни одной части с текстом.")
    return out[:6]


def step_short(ctx: StepContext) -> None:
    if is_post(ctx):
        raise Skip("У поста одно видео: формат 9:16 — кадр «как постер» с вертикальной картинкой.")
    if not ctx.settings["shorts"]:
        raise Skip("Шортс выключен в настройках.")
    text = require_text(ctx)
    existing = ctx.read(SHORT)
    if ctx.state.get("keep") and existing:
        beats = parse_short(json.loads(existing))  # сценарий поправил человек — не переписываем
    else:
        beats = parse_short(extract_json(llm(ctx, prompts.short(text, ctx.project["title"], SHORT_WORDS))))
        ctx.write(SHORT, json.dumps({"beats": beats}, ensure_ascii=False, indent=2) + "\n")
    ctx.remember(keep=False)
    script = "\n\n".join(b["text"] for b in beats) + "\n"
    ctx.write("short/script.md", script)
    audio = speak(ctx, script, ctx.path("short/voice.mp3"), "Озвучка шортса")
    # Части шортса — картинки основного видео по кругу; нет картинок — крупный заголовок.
    plan = [slides.Slide(para=n, title=b["title"] or f"Часть {n + 1}") for n, b in enumerate(beats)]
    render_video(ctx, text=script, plan=plan, audio=audio, out=ctx.path(SHORT_VIDEO), orientation=VERTICAL, cycle=True)


# published.json — журнал публикации: запись на канал, у поста «картинка и аудио» — и на каждую часть.
# Состояние пишется до отправки (sending) и сразу после ответа (sent / failed / unknown), а не одной
# записью в конце: сбой второго канала или рестарт посреди цикла не теряют отметку о первом.
SENDING, SENT, UNKNOWN, SEND_FAILED = "sending", "sent", "unknown", "failed"
UNCLEAR = (SENDING, UNKNOWN)  # ушёл ли пост — неизвестно: решает человек
UNCLEAR_NOTE = "Статус неизвестен — проверь канал"


# Пакет для ручной загрузки (YouTube, TikTok…): packaged — файлы готовы, uploaded — автор залил и
# дал ссылку на ролик. Опубликован только uploaded: пакет мог так и не дойти до площадки (AN-003).
PACKAGED, UPLOADED = "packaged", "uploaded"


def send_state(record: dict) -> str:
    """Состояние записи; записи до журнала — sent, если в них есть время отправки."""
    return record.get("state") or (SENT if record.get("sent") else SEND_FAILED)


def publication_state(record: dict) -> str:
    """Состояние записи с учётом пакетов: пакет без состояния (до AN-003) — packaged."""
    if record.get("state") in (PACKAGED, UPLOADED):
        return record["state"]
    if record.get("files") and not record.get("message_id") and not record.get("state"):
        return PACKAGED
    return send_state(record)


def is_live(record: dict) -> bool:
    """Пост действительно вышел: ушёл в Telegram или залит руками со ссылкой."""
    return publication_state(record) in (SENT, UPLOADED)


def is_unclear(record: dict | None) -> bool:
    """Ушёл ли пост (или его часть) — неизвестно: была отправка, а ответа нет."""
    if not record:
        return False
    if record.get("parts"):
        return any(p.get("state") in UNCLEAR for p in record["parts"].values())
    return send_state(record) in UNCLEAR and not record.get("message_id")


def resolve_record(record: dict, posted: bool) -> dict:
    """Решение человека по неясной записи: «Пост есть» — sent, «Поста нет» — failed (повтор дошлёт)."""
    now = time.time()
    parts = {
        name: (part | {"state": SENT if posted else SEND_FAILED, "resolved": True, "at": now})
        if part.get("state") in UNCLEAR
        else part
        for name, part in (record.get("parts") or {}).items()
    }
    out = {k: v for k, v in record.items() if k != "skipped"} | {"resolved": True, "at": now}
    if parts:
        out["parts"] = parts
    if posted and (not parts or all(parts.get(name, {}).get("state") == SENT for name in publish.PHOTO_PARTS)):
        return out | {"state": SENT, "ok": True, "sent": now, "message": "Пост есть — отмечено вручную."}
    message = (
        "Отмечено вручную: часть поста есть, повтор дошлёт остальное."
        if posted
        else ("Поста нет — отмечено вручную, повтор отправит.")
    )
    return out | {"state": SEND_FAILED, "ok": False, "message": message}


class Journal:
    """published.json как журнал: файл переписывается атомарно при каждой смене состояния записи."""

    def __init__(self, ctx: StepContext) -> None:
        self.ctx = ctx
        self.records = read_published(ctx)

    def get(self, channel_id: str) -> dict | None:
        return next((r for r in self.records if r["channel"] == channel_id), None)

    def put(self, record: dict) -> dict:
        # Запись канала — на своём месте, новый канал — в конец: порядок как в выборе каналов.
        same = [n for n, r in enumerate(self.records) if r["channel"] == record["channel"]]
        if same:
            self.records[same[0]] = record
        else:
            self.records.append(record)
        self.ctx.write(PUBLISHED, json.dumps(self.records, ensure_ascii=False, indent=2) + "\n")
        return record


def step_publish(ctx: StepContext) -> None:
    svc = services(ctx)
    chosen = ctx.settings.get("channels") or []
    if not chosen:
        raise Waiting("Выбери каналы и нажми «Опубликовать».")
    if not ctx.settings.get("auto_publish") and not ctx.state.get("approved"):
        raise Waiting("Посмотри видео, выбери каналы и нажми «Опубликовать».")
    if verdict(ctx.read(NARRATOR_REVIEW)) == NOT_READY and not ctx.state.get("ignore_verdict"):
        raise Waiting(
            "Судья сказал «не готово» — см. его отчёт в шаге «Текст». Поправь выпуск или включи "
            "«Опубликовать несмотря на вердикт» и нажми «Опубликовать»."
        )
    force = bool(ctx.state.get("force"))
    approved = bool(ctx.state.get("approved"))
    # Подтверждение одноразовое: чем бы ни кончился шаг, «Запустить всё» или перезапуск
    # без нового нажатия «Опубликовать» ничего не отправят (и не пропустят чужие ссылки).
    ctx.remember(force=False, approved=False)
    channels = {c.id: c for c in svc.config.channels()}
    plan_title = ""
    if ctx.read(PLAN):
        plan_title = json.loads(ctx.read(PLAN)).get("title", "")
    title = plan_title or ctx.project["title"]
    off = disclaimers_off(ctx, title)
    post = with_source(ctx, ctx.read(POST))
    if ctx.settings.get("disclaimers", True):
        post = registry.with_footnotes(post, f"{title}\n{ctx.read(TEXT)}", banned(svc.config))
    links = post_links(ctx.project["scenario"], ctx.settings, ctx.read, post)
    if links["domains"]:
        ctx.log("ссылки в посте ведут на: " + ", ".join(links["domains"]))
    # Автопубликация не пропускает ссылки, которых нет в исходнике: LLM мог их подменить (SEC-013).
    if links["foreign"] and not approved:
        raise Waiting(
            "В посте ссылки на домены, которых нет в исходном материале: "
            + ", ".join(links["foreign"])
            + ". Проверь ссылки и нажми «Опубликовать», если всё верно."
        )
    # Сначала все каналы: что не уйдёт — видно сразу, и ни один канал не получит выпуск наполовину.
    problems = []
    photo_post = ctx.settings.get("post_format") == "photo"
    for channel_id in chosen:
        channel = channels.get(channel_id)
        if channel is None:
            problems.append(f"канала {channel_id} нет в {svc.config.channels_file.name}")
            continue
        if channel.type == publish.TELEGRAM and channel.problem():
            problems.append(f"{channel.label}: {channel.problem()}")
        elif not (channel.type == publish.TELEGRAM and photo_post):
            video = ctx.path(SHORT_VIDEO if channel.format == VERTICAL else VIDEO)
            if not video.is_file():
                hint = "включи «Шортс» и собери его" if channel.format == VERTICAL else "собери видео"
                problems.append(f"{channel.label}: нет видео — {hint}")
    if problems:
        raise StudioError("Ничего не отправлено: " + "; ".join(problems) + ".")
    journal = Journal(ctx)
    # Неясные записи (отправка была, ответа нет) повтор не трогает, пока человек не проверит канал.
    unclear = [channels[c].label for c in chosen if not force and is_unclear(journal.get(c))]
    if unclear:
        raise Waiting(unclear_note(unclear))
    for n, channel_id in enumerate(chosen, start=1):
        channel = channels[channel_id]
        prev = journal.get(channel_id)
        # Как `news publish` у narrator: в канал, куда уже ушло, повторно — только с «Опубликовать повторно».
        if channel.type == publish.TELEGRAM and prev and send_state(prev) == SENT and not force:
            message = "уже опубликовано — включи «Опубликовать повторно», чтобы отправить ещё раз"
            ctx.log(f"{channel.label}: {message}")
            journal.put(prev | {"label": channel.label, "skipped": True, "message": message})
            continue
        ctx.progress(f"{channel.label} ({n} из {len(chosen)})", 100.0 * (n - 1) / len(chosen))
        if channel.type == publish.TELEGRAM and photo_post:
            record = publish_photo(ctx, journal, channel, title, post, None if force else prev)
        elif channel.type == publish.TELEGRAM:
            record = publish_video(ctx, journal, channel, title, post)
        else:
            video = ctx.path(SHORT_VIDEO if channel.format == VERTICAL else VIDEO)
            result = publish.publish(channel, video=video, title=title, post=post, out_dir=ctx.path("publish"))
            if prev and publication_state(prev) == UPLOADED:
                # Уже залит: пакет пересобран, но ссылка и время публикации остаются.
                record = journal.put(prev | {"files": result.get("files"), "label": channel.label})
            else:
                fresh = {"state": PACKAGED, "at": time.time(), "attempt_id": uuid.uuid4().hex[:12]}
                record = journal.put(result | {"label": channel.label} | fresh)
        if off:  # осознанно без пометок: когда, почему и о ком — в записи публикации
            record = journal.put(record | {"disclaimers_off": off})
        ctx.log(f"{channel.label}: {record['message']}")
        log.event(
            "publication",
            project=ctx.project["id"],
            step=ctx.step,
            channel=channel_id,
            message_id=record.get("message_id"),
            state=record.get("state"),
        )
        again = channel.type == publish.TELEGRAM and force and prev is not None and send_state(prev) == SENT
        published_event(ctx, channel, record, again=again)


def published_event(ctx: StepContext, channel: publish.Channel, record: dict, *, again: bool) -> None:
    """Публикация в журнал событий: ушло в Telegram (или повторно) либо готов пакет для ручной загрузки."""
    if channel.type == publish.MANUAL:
        event = "publication.packaged"
    else:
        event = "publication.republished" if again else "publication.sent"
    ctx.emit(
        event,
        channel_id=channel.id,
        publication_id=record.get("attempt_id"),
        source=channel.type,
        props={"format": channel.format, "message_id": record.get("message_id")},
    )


def unclear_note(labels: list[str]) -> str:
    return (
        f"{UNCLEAR_NOTE}: {', '.join(labels)}. Telegram не ответил на отправку — пост мог уйти. "
        "Нажми «Пост есть» или «Поста нет»: повтор отправит только недостающее."
    )


def journaled(journal: Journal, channel: publish.Channel, record: dict, part: str | None, send) -> dict:
    """Одна отправка под журналом: до — sending, после — ответ Telegram; без ответа — unknown и ожидание.

    send() возвращает id сообщения. part — часть поста «картинка и аудио» (её состояние — в parts;
    запись целиком станет sent, только когда уйдут все части). Возвращает запись после ответа."""

    def mark(state: str, message_id: int | None = None, message: str | None = None) -> dict:
        nonlocal record
        now = time.time()
        top = state if part is None or state != SENT else SENDING
        record = record | {"state": top, "at": now} | ({"message": message} if message else {})
        if part is None:
            record |= {"message_id": message_id} if message_id is not None else {}
        else:
            entry = {"state": state, "at": now} | ({"message_id": message_id} if message_id is not None else {})
            record = record | {"parts": dict(record.get("parts") or {}) | {part: entry}}
        return journal.put(record)

    def failed(state: str, exc: Exception) -> None:
        journal.ctx.emit(
            "publication.failed",
            channel_id=channel.id,
            publication_id=record.get("attempt_id"),
            source=channel.type,
            error_class=type(exc).__name__,
            props={"state": state, "part": part},
        )

    mark(SENDING)
    try:
        message_id = send()
    except publish.Unknown as exc:
        mark(UNKNOWN, message=f"{exc}.")
        failed(UNKNOWN, exc)
        raise Waiting(unclear_note([channel.label])) from exc
    except StudioError as exc:
        mark(SEND_FAILED, message=str(exc))
        failed(SEND_FAILED, exc)
        raise
    except Exception as exc:
        # Сбой уже после начала отправки: ушёл ли пост — неизвестно, повтор сам не шлём.
        mark(UNKNOWN, message=f"Сбой при отправке: {exc!r}.")
        failed(UNKNOWN, exc)
        raise Waiting(unclear_note([channel.label])) from exc
    return mark(SENT, message_id=message_id)


def publish_video(ctx: StepContext, journal: Journal, channel: publish.Channel, title: str, post: str) -> dict:
    svc = services(ctx)
    video = ctx.path(SHORT_VIDEO if channel.format == VERTICAL else VIDEO)
    width, height = media.video_size(video, svc.config.ffmpeg)
    seconds = media.duration(video, svc.config.ffmpeg)
    cover, thumb = covers(video)
    result: dict = {}

    def send() -> int | None:
        result.update(
            publish.publish(
                channel,
                video=video,
                title=title,
                post=post,
                out_dir=ctx.path("publish"),
                width=width,
                height=height,
                seconds=seconds,
                cover=cover if cover.is_file() else None,
                thumb=thumb if thumb.is_file() else None,
            )
        )
        return result.get("message_id")

    record = {
        "channel": channel.id,
        "label": channel.label,
        "ok": False,
        "attempt_id": uuid.uuid4().hex[:12],
        "message": "Отправляется…",
    }
    record = journaled(journal, channel, record, None, send)  # уже sent в журнале — дальше ничего не теряем
    return journal.put(record | result | {"ok": True, "state": SENT, "sent": time.time()} | audience(channel))


def source_line(ctx: StepContext) -> str:
    """«Источник: автор — ссылка» для «Видео по статье»; пусто, пока источник не указан."""
    url, author = ctx.settings.get("source_url") or "", ctx.settings.get("source_author") or ""
    return f"Источник: {author} — {url}" if url and author else ""


def link_sources(scenario: str, settings: dict, read) -> list[str]:
    """Исходный материал выпуска, откуда законно берутся ссылки поста (SEC-013)."""
    sources = [settings.get("source_url") or "", read(TEXT)]
    folder = read(NARRATOR_FOLDER).strip() if scenario == "digest" else ""
    if folder:  # дайджест: ссылки материалов из базы narrator и текст выпуска
        for name in ("events.md", "text.md"):
            path = Path(folder) / name
            if path.is_file():
                sources.append(path.read_text(encoding="utf-8"))
    return sources


def post_links(scenario: str, settings: dict, read, post: str) -> dict:
    """Домены ссылок поста и те из них, которых нет в исходнике; пост «Пост в канал» пишет человек."""
    foreign = [] if scenario == POST_SCENARIO else publish.foreign_domains(post, link_sources(scenario, settings, read))
    return {"domains": publish.link_domains(post), "foreign": foreign}


def with_source(ctx: StepContext, post: str) -> str:
    """Пост «Видео по статье» с источником в неудаляемом хвосте; без источника — отказ (CMP-005)."""
    if ctx.project["scenario"] != "article":
        return post
    url, author = ctx.settings.get("source_url") or "", ctx.settings.get("source_author") or ""
    if not url or not author:
        raise StudioError(
            "Видео по чужой статье публикуется только с источником: укажи ссылку на оригинал и автора "
            "в настройках выпуска (раздел «Источник») и нажми «Опубликовать» снова."
        )
    if url in post:
        return post
    return f"{post.rstrip()}\n\nИсточник: {author.replace('*', '')} — [оригинал]({url})\n"


def disclaimers_off(ctx: StepContext, title: str) -> dict | None:
    """Пометки выключены, а в выпуске есть упоминания из реестра: публикация — только с причиной."""
    if ctx.settings.get("disclaimers", True):
        return None
    said = "\n".join([title, ctx.project["title"], ctx.read(TEXT), ctx.read(POST)])
    names = [e.name for e in registry.mentioned(said, banned(services(ctx).config))]
    if not names:
        return None
    reason = " ".join(str(ctx.state.get("ignore_disclaimers") or "").split())
    if not reason:
        raise StudioError(
            f"В тексте есть упоминания из реестра ({', '.join(names)}), пометки выключены. Включи "
            "«Помечать запрещённые организации» и пересобери видео или подтверди публикацию без пометок с причиной."
        )
    ctx.log(f"публикация без пометок о {', '.join(names)} — причина: {reason}")
    return {"reason": reason, "at": time.time(), "mentions": names}


def audience(channel: publish.Channel) -> dict:
    """Подписчики канала в момент публикации — от них считается прирост в «Что работает»."""
    return {"members_at_publish": publish.members(channel)}


def publish_photo(
    ctx: StepContext, journal: Journal, channel: publish.Channel, title: str, post: str, prev: dict | None
) -> dict:
    """Вид поста «картинка и аудио»: постер с подписью, затем mp3 озвучки.

    Каждая часть отмечается в журнале отдельно: повтор досылает только недостающие."""
    poster = find(ctx.dir, "poster", IMAGE_EXT)
    audio = find(ctx.dir, "voice", AUDIO_EXT)
    if poster is None or audio is None:
        raise StudioError(f"{channel.label}: для поста «картинка и аудио» нужны постер и озвучка.")
    audio = paced(ctx, audio)
    problem = publish.photo_problem(channel, poster, audio)
    if problem:
        raise StudioError(problem)
    seconds = media.duration(audio, services(ctx).config.ffmpeg)
    done = {k: v for k, v in ((prev or {}).get("parts") or {}).items() if v.get("state") == SENT}
    if prev and done:
        record = {k: v for k, v in prev.items() if k != "skipped"} | {"parts": done}
    else:
        record = {"channel": channel.id, "attempt_id": uuid.uuid4().hex[:12]}
    record |= {"label": channel.label, "ok": False, "message": "Отправляется…"}
    for part in publish.PHOTO_PARTS:
        if part in done:
            ctx.log(f"{channel.label}: {'картинка' if part == 'photo' else 'аудио'} уже в канале — не повторяем")
            continue

        def send(part: str = part) -> int | None:
            return publish.send_photo_part(
                channel, part, poster=poster, audio=audio, title=title, post=post, seconds=seconds
            )

        record = journaled(journal, channel, record, part, send)
    ids = {name: p.get("message_id") for name, p in record["parts"].items()}
    done_record = record | publish.photo_result(channel, ids) | {"state": SENT, "sent": time.time()}
    return journal.put(done_record | audience(channel))


def read_published(ctx: StepContext) -> list[dict]:
    return load_published(ctx.dir)


def load_published(folder: Path) -> list[dict]:
    try:
        data = json.loads((folder / PUBLISHED).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [r for r in data if isinstance(r, dict) and r.get("channel")] if isinstance(data, list) else []


def save_published(folder: Path, records: list[dict]) -> None:
    tmp = folder / (PUBLISHED + ".tmp")
    tmp.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(folder / PUBLISHED)


STEPS = {
    "text": step_text,
    "poster_prompt": step_poster_prompt,
    "poster": step_poster,
    "plan": step_plan,
    "images": step_images,
    "clips": step_clips,
    "voice": step_voice,
    "render": step_render,
    "short": step_short,
    "publish": step_publish,
}
