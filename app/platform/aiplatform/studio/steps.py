"""Шаги сценариев «Дайджест новостей» и «Видео по статье».

Файлы проекта:
  text.md            — текст для озвучки (пересказ новостей или статья)
  post.md            — подпись к посту
  poster_prompt.md   — промпт постера (первого кадра)
  poster.png|jpg|webp — постер: сгенерирован локально или загружен
  narrator/image_prompt.md, narrator/cover.* — промпт и картинка обложки из выпуска narrator (дайджест)
  plan.json          — {title, post, slides: [{para, title, bullets, image_prompt}]}
  images/NN.png|jpg|webp — картинки слайдов (+ NN.json — каким промптом, uploaded — загрузил человек)
  clips/NN.mp4|mov|webm  — видео-фоны слайдов: из вкладки «Видео» или от человека (+ NN.json)
  voice.mp3|wav|m4a  — озвучка
  video.mp4          — 16:9
  video-cover.jpg, video-thumb.jpg — первый кадр (обложка для Telegram) и миниатюра, если есть оформление
  short.json, short/voice.mp3, short.mp4 — шортс 9:16
  published.json     — куда и что отправлено

Если в тексте упомянута организация из реестра запрещённых в РФ
(updates/registry.py), на её слайдах внизу идёт мелкая пометка, а в подпись
к посту она дописывается в конце. Выключается настройкой disclaimers.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

from aiplatform.core import media
from aiplatform.core.runner import Canceled
from aiplatform.studio import news, prompts, publish, slides
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
NARRATOR_PROMPT = "narrator/image_prompt.md"
NARRATOR_COVER = "narrator/cover"
NARRATOR_FOLDER = "narrator/folder.txt"  # каталог выпуска: туда же человек кладёт обложку из ChatGPT
NARRATOR_COVER_EXT = (".png", ".jpg", ".jpeg", ".webp")
CLIP_PRESET = "landscape-m"  # 704×448 из пресетов вкладки «Видео»: быстрее, а ffmpeg растянет до кадра
CLIP_DURATION_S = 5  # есть у всех видеомоделей; длиннее слайд — ролик идёт по кругу
MAX_CLIPS = 3  # роликов на выпуск: ключевые моменты, остальные слайды — картинка с наездом
UPSCALE_TARGETS = ("2k", "4k")
TEMPOS = (0.75, 1.0, 1.25, 1.5, 1.75, 2.0)  # скорость озвучки в видео, как DOWNLOAD_TEMPOS у narrator
SHORT_WORDS = 140
POLL_S = 0.3


@dataclass
class Services:
    config: StudioConfig
    photo: object  # photo.jobs.JobManager: create / wait / image_path
    audio: object  # audio.jobs.AudioJobs: create / wait / audio_path
    video: object  # video.jobs.JobManager: create / get / cancel / video_path
    renderer: Renderer


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


def generate_image(ctx: StepContext, prompt: str, target: Path, what: str, size: str = IMAGE_SIZE) -> str:
    """Картинка во вкладке «Фото» (и увеличение, если включено). Возвращает id исходной задачи."""
    svc = services(ctx)
    payload = {"mode": "txt2img", "prompt": prompt, "size": size, "model": ctx.settings["image_model"]}
    try:
        job = svc.photo.create(payload)
    except ValueError as exc:
        raise StudioError(f"{what}: {exc}") from exc
    done = wait_job(ctx, "photo", svc.photo, job, what)
    finish_image(ctx, done["id"], target, what)
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
    return target


def speak(ctx: StepContext, text: str, target: Path, what: str) -> Path:
    svc = services(ctx)
    settings = ctx.settings
    payload = {"text": text, "engine": settings["engine"], "voice": settings.get("voice") or None}
    if settings.get("speed"):
        payload["speed"] = settings["speed"]
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
        end = plan[n + 1].para if n + 1 < len(plan) else len(paras)
        said = "\n".join([*paras[slide.para : max(end, slide.para + 1)], slide.title, *slide.bullets])
        out.append(registry.notes(said, entries))
    return out


def load_plan(ctx: StepContext) -> tuple[dict, list[slides.Slide]]:
    raw = ctx.read(PLAN)
    if not raw:
        raise StudioError("Нет плана слайдов — выполни шаг «Слайды».")
    data = json.loads(raw)
    paras = slides.paragraphs(require_text(ctx))
    return data, slides.parse_plan(data, len(paras))


# --- шаги ---


def step_text(ctx: StepContext) -> None:
    if ctx.project["scenario"] == "digest":
        day = ctx.project["day"]
        ctx.progress(f"narrator собирает новости за {day}…")
        folder = news.collect(day, services(ctx).config, ctx.cancel, lambda line: line.strip() and ctx.log(line))
        ctx.write(TEXT, (folder / news.NARRATION).read_text(encoding="utf-8"))
        post = folder / news.POST
        if post.is_file():
            ctx.write(POST, post.read_text(encoding="utf-8"))
        take_narrator_cover(ctx, folder)
        ctx.log(f"выпуск: {folder}")
    text = require_text(ctx)
    words = len(text.split())
    ctx.log(f"{words} слов, {len(slides.paragraphs(text))} абзацев, ~{max(round(words / 130), 1)} мин озвучки")
    if ctx.settings.get("review_text") and not ctx.state.get("approved"):
        raise Waiting("Проверь и при необходимости поправь текст, затем нажми «Продолжить».")


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
    text = require_text(ctx)
    paras = slides.paragraphs(text)
    answer = llm(ctx, prompts.plan(paras, ctx.project["title"], slides.max_slides(len(paras))))
    data = extract_json(answer)
    plan = slides.parse_plan(data, len(paras))
    title = str(data.get("title") or "").strip() if isinstance(data, dict) else ""
    post = str(data.get("post") or "").strip() if isinstance(data, dict) else ""
    out = {"title": title or ctx.project["title"], "post": post, "slides": [s.to_dict() for s in plan]}
    ctx.write(PLAN, json.dumps(out, ensure_ascii=False, indent=2) + "\n")
    if post and not ctx.read(POST).strip():
        ctx.write(POST, post + "\n")
    if title and ctx.project.get("auto_title"):
        ctx.set_title(title)
    ctx.log(f"{len(plan)} слайдов")


def step_images(ctx: StepContext) -> None:
    if not ctx.settings["draw_images"]:
        raise Skip("Картинки выключены: фон слайдов — градиент.")
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
    photo = services(ctx).photo
    for i, (n, prompt) in enumerate(todo, start=1):
        meta_path = folder / f"{n:02d}.json"
        meta = read_meta(meta_path)
        if meta.get("uploaded") and slide_media(folder, n, IMAGES, prompt):
            continue  # свою картинку человек положил сам — не перерисовываем
        target, what = folder / f"{n:02d}.png", f"Картинка слайда {n}"
        same = meta.get("prompt") == prompt and meta.get("model") == model and target.is_file()
        if same and meta.get("upscale", "none") == upscale:
            continue
        ctx.progress(f"Картинка {i} из {len(todo)}", 100.0 * (i - 1) / len(todo))
        job_id = meta.get("job")
        if same and job_id and photo.image_path(job_id) is not None:
            finish_image(ctx, job_id, target, what)  # поменялось только увеличение — не перерисовываем
        else:
            for old in (folder / f"{n:02d}{ext}" for ext in IMAGE_EXT):
                old.unlink(missing_ok=True)
            job_id = generate_image(ctx, prompt, target, what)
        want = {"prompt": prompt, "model": model, "job": job_id, "upscale": upscale}
        meta_path.write_text(json.dumps(want, ensure_ascii=False), encoding="utf-8")


def step_clips(ctx: StepContext) -> None:
    if not ctx.settings.get("clips"):
        raise Skip("Ролики выключены: фон слайдов — картинки с медленным наездом.")
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

    out = []
    for n, (slide, seconds) in enumerate(zip(plan, durations, strict=True), start=1):
        out.append(
            {
                "title": slide.title,
                "bullets": slide.bullets,
                "image": pick(IMAGES, n),
                "clip": pick(CLIPS, n),
                "notes": notes[n - 1] if notes else [],
                "seconds": seconds,
            }
        )
    return out


def render_video(
    ctx: StepContext, *, text: str, plan: list[slides.Slide], audio: Path, out: Path, orientation: str, cycle: bool
) -> None:
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
    durations = slides.durations(paras, plan, audio_s, intro)
    notes = slide_notes(paras, plan, banned(svc.config)) if settings.get("disclaimers", True) else None
    marked = [n for n, found in enumerate(notes or [], start=1) if found]
    if marked:
        ctx.log(f"пометки о запрещённых организациях: слайды {', '.join(map(str, marked))}")
    scene_list = scenes(
        ctx.dir, plan, durations, cycle, images=settings["draw_images"], clips=bool(settings.get("clips")), notes=notes
    )

    def on_progress(done: int, total: int) -> None:
        ctx.progress(f"Кадры: {done} из {total}", 90.0 * done / total)

    ctx.progress("Рисую кадры…", 0.0)
    svc.renderer.build(
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
    ctx.log(f"{out.name}: {audio_s:.0f} с, {len(scene_list)} слайдов")


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


def step_render(ctx: StepContext) -> None:
    text = require_text(ctx)
    _, plan = load_plan(ctx)
    audio = find(ctx.dir, "voice", AUDIO_EXT)
    if audio is None:
        raise StudioError("Нет озвучки — выполни шаг «Озвучка».")
    render_video(ctx, text=text, plan=plan, audio=audio, out=ctx.path(VIDEO), orientation=LANDSCAPE, cycle=False)


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
    plan = [slides.Slide(para=n, title=b["title"] or f"Часть {n + 1}") for n, b in enumerate(beats)]
    render_video(ctx, text=script, plan=plan, audio=audio, out=ctx.path(SHORT_VIDEO), orientation=VERTICAL, cycle=True)


def step_publish(ctx: StepContext) -> None:
    svc = services(ctx)
    chosen = ctx.settings.get("channels") or []
    if not chosen:
        raise Waiting("Выбери каналы публикации в настройках и нажми «Опубликовать».")
    if not ctx.settings.get("auto_publish") and not ctx.state.get("approved"):
        raise Waiting("Посмотри видео и нажми «Опубликовать».")
    channels = {c.id: c for c in publish.load_channels(svc.config.channels_file)}
    plan_title = ""
    if ctx.read(PLAN):
        plan_title = json.loads(ctx.read(PLAN)).get("title", "")
    title = plan_title or ctx.project["title"]
    post = ctx.read(POST)
    if ctx.settings.get("disclaimers", True):
        post = registry.with_footnotes(post, f"{title}\n{ctx.read(TEXT)}", banned(svc.config))
    results = []
    for n, channel_id in enumerate(chosen, start=1):
        channel = channels.get(channel_id)
        if channel is None:
            raise StudioError(f"Канала {channel_id} нет в {svc.config.channels_file.name}.")
        video = ctx.path(SHORT_VIDEO if channel.format == VERTICAL else VIDEO)
        if not video.is_file():
            hint = "включи «Шортс» и собери его" if channel.format == VERTICAL else "собери видео"
            raise StudioError(f"{channel.label}: нет видео — {hint}.")
        ctx.progress(f"{channel.label} ({n} из {len(chosen)})", 100.0 * (n - 1) / len(chosen))
        width, height = media.video_size(video, svc.config.ffmpeg)
        cover, thumb = covers(video)
        result = publish.publish(
            channel,
            video=video,
            title=title,
            post=post,
            out_dir=ctx.path("publish"),
            width=width,
            height=height,
            seconds=media.duration(video, svc.config.ffmpeg),
            cover=cover if cover.is_file() else None,
            thumb=thumb if thumb.is_file() else None,
        )
        ctx.log(f"{channel.label}: {result['message']}")
        results.append(result | {"label": channel.label})
    ctx.write(PUBLISHED, json.dumps(results, ensure_ascii=False, indent=2) + "\n")


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
