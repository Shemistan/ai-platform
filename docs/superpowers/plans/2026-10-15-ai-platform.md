---
тип: plan
статус: в работе
обновлено: 2026-10-15
спека: 2026-10-15-ai-platform-design.md
---

# AI Platform Implementation Plan

> **For agentic workers:** исполнение — Native (агент сам, задача за задачей,
> каждая задача = подзадача регламента: `./check` → коммит → `./finish`).

**Goal:** один локальный сервис с вкладками Фото / Видео / Аудио / Дайджест /
Видео по статье, общим GPU-замком и пайплайном сценариев до публикации.

**Architecture:** Python stdlib `ThreadingHTTPServer`, пакет `aiplatform` в
`app/platform/`. Фото и видео — перенос кода photogen/videogen под префиксы
`/api/photo`, `/api/video`; аудио и сбор новостей — через CLI narrator;
сценарии — проекты на диске с шагами; сборка видео — ffmpeg.

**Tech Stack:** Python ≥3.11 (stdlib), ванильный JS без сборки, ffmpeg,
`unittest`, ruff (если установлен).

**Spec:** `docs/superpowers/specs/2026-10-15-ai-platform-design.md`

## Global Constraints

- Только stdlib Python: `pyproject.toml` → `dependencies = []`.
- Сервис слушает `127.0.0.1`, порт `AIP_PORT` (по умолчанию 8780).
- `AIP_BACKEND=fake` — все модели, TTS, LLM и новости фейковые; `make demo`.
- POST с чужого `Origin` → 403; ошибки — `{"error": "<русский текст>"}`.
- Одна тяжёлая модель за раз: каждый запуск модели — под `core.gate.Gate`.
- Секреты (токен Telegram) — только из env, не в файлы/логи/ответы API.
- Тесты: `make test` = `python3 -W error::ResourceWarning -m unittest discover -s tests -t .`.

## Review Focus

1. Сценарий с текстом без пустых строк (один абзац) → один слайд, видео собирается.
2. Слайд-план от LLM с битым JSON / номерами абзацев вне диапазона → шаг падает с понятной ошибкой либо номера зажимаются в диапазон, а не 500.
3. Нет ffmpeg на машине → `/api/config` показывает проблему, шаг «Сборка» падает с текстом «нет ffmpeg», сервер жив.
4. Повторное «Запустить всё» при уже идущем запуске → 409, а не два параллельных прогона.
5. Публикация в Telegram без токена в env → ошибка шага «нет токена AIP_…», без утечки токена в лог.

---

### Task 1: Ядро и оболочка вкладок

**Files:** `app/platform/{Makefile,pyproject.toml,README.md}`,
`aiplatform/{__init__,config,server}.py`, `aiplatform/core/{runner,uploads,gate,http}.py`,
`aiplatform/static/{index.html,nav.js,common.css}`, `tests/test_core.py`, `tests/test_server_shell.py`.

**Interfaces — Produces:**
- `core.gate.Gate()`; `with gate.hold(owner: str, on_wait: Callable[[str], None] | None = None, cancel: threading.Event | None = None)` — FIFO; при `cancel.set()` во время ожидания бросает `core.runner.Canceled`. `gate.holder() -> str | None`.
- `core.runner.run(argv, on_line, cancel, *, cwd=None, env=None)`, `Canceled`, `RunFailed` (перенос из photogen).
- `core.uploads.UploadStore(dir)`: `save(data) -> id`, `path(id)`, `size(id)`; `UploadError`, `MIME`, `MAX_UPLOAD_BYTES` (перенос photogen + аудио-типы mp3/wav/m4a).
- `core.http`: `HttpError(status, message)`; `Request` (method, parts, query, headers, `body(limit)`, `json(limit)`); `Response` helpers `json(...)`, `file(path, ctype, *, attachment=None, cache=...)` с поддержкой Range.
- `server.Module` протокол: `prefix: str`, `handle(req) -> bool`, `describe() -> dict`, `close()`; `make_server(config, modules=None) -> (ThreadingHTTPServer, App)`.
- `config.Config` с `data_dir`, `host`, `port`, `fake`, `ffmpeg`, `font`, под-конфиги вкладок.

- [ ] Тесты: gate FIFO (2 потока, порядок), отмена ожидания, holder; `/` → редирект на `/photo/`; `/api/config` → `{"tabs": [...], "problems": [...]}`; POST с чужим Origin → 403; неизвестный `/api/x` → 404.
- [ ] Реализация, `make check` зелёный, коммит `feat(platform): ядро — сервер, GPU-замок, загрузки, вкладки`.

### Task 2: Вкладка «Фото»

**Files:** `aiplatform/photo/{__init__,models,params,progress,mflux,flux2,jobs,service}.py`,
`aiplatform/fakes/fake_gen.py`, `aiplatform/static/photo/{index.html,app.js,style.css}`,
`tests/test_photo_params.py`, `tests/test_photo_pipelines.py`, `tests/test_photo_server.py` (перенос тестов photogen).

**Interfaces — Consumes:** Task 1 (`Gate`, `runner`, `UploadStore`, `core.http`).
**Produces:** `photo.service.PhotoModule(config, uploads, gate)` (Module) с
`jobs: JobManager`; `JobManager.submit_prompt(prompt: str, width: int, height: int, *, model: str | None = None) -> dict`; `JobManager.wait(job_id, cancel) -> dict` (финальный view); `JobManager.image_path(job_id) -> Path | None`.

- [ ] Перенести модули photogen, заменить `photogen.` → `aiplatform.photo.` / `aiplatform.core.`; воркер берёт `gate.hold("Фото", ...)`; URL-ы `/api/photo/...`.
- [ ] Перенести тесты photogen (params, pipelines, server) на новый префикс; добавить тест «пока замок у другого — стадия „Ждёт GPU“».
- [ ] UI: `static/photo/` из photogen, `api()` с базой `/api/photo`, общая шапка `nav.js`.
- [ ] Коммит `feat(photo): вкладка фото — перенос photogen`.

### Task 3: Вкладка «Видео»

**Files:** `aiplatform/video/{__init__,models,timeline,living,ltx,wan,jobs,service}.py`,
`aiplatform/fakes/{fake_ltx,fake_wan}.py`, `aiplatform/static/video/*`,
`tests/test_video_{timeline,ltx,wan,living,server}.py`.

**Produces:** `video.service.VideoModule(config, uploads, gate)`.

- [ ] Перенос videogen по той же схеме (префикс `/api/video`, замок «Видео»).
- [ ] Перенос тестов; коммит `feat(video): вкладка видео — перенос videogen`.

### Task 4: Вкладка «Аудио»

**Files:** `aiplatform/audio/{__init__,voices,jobs,service}.py`, `aiplatform/fakes/fake_narrate.py`,
`aiplatform/core/media.py` (ffprobe-длительность, тишина/тон через ffmpeg),
`aiplatform/static/audio/*`, `tests/test_audio.py`.

**Produces:**
- `audio.voices.ENGINES: dict[str, Engine(label, voices: tuple[str,...], default_voice)]`, `DEFAULT_ENGINE`.
- `audio.jobs.build_spec(payload) -> AudioSpec(text, engine, voice, speed)`; `AudioJobs.submit(spec) -> dict`, `wait(job_id, cancel) -> dict`, `audio_path(job_id) -> Path | None`.
- `core.media.duration(path) -> float` (ffprobe; 0.0 если нельзя), `core.media.ffmpeg_available() -> bool`.
- argv: `[*narrate_cmd, "say", text_file, "--engine", E, "--voice", V, "--speed", S, "--out", out_mp3, "--no-verify"]`.

- [ ] Тесты: валидация (пустой текст, неизвестный движок/голос, скорость вне 0.5–2), задача через фейк до `done` с mp3 и `duration_s>0`, HTTP `/api/audio/jobs`, скачивание.
- [ ] Коммит `feat(audio): вкладка озвучки через narrator`.

### Task 5: Движок сценариев и «Видео по статье»

**Files:** `aiplatform/studio/{__init__,pipeline,steps,llm,slides,assemble,service}.py`,
`aiplatform/fakes/fake_llm.py`, `aiplatform/static/studio/*`,
`tests/test_studio_{slides,llm,assemble,pipeline}.py`.

**Interfaces — Consumes:** `PhotoModule.jobs`, `AudioJobs`, `core.media`.
**Produces:**
- `studio.pipeline.Project` (на диске `project.json`): `id, scenario, title, settings, steps: dict[name, StepState(status, error, log, updated_at)]`; статусы `pending|running|done|failed|waiting`.
- `studio.pipeline.Studio(config, ctx)`: `create(scenario, payload) -> dict`, `get(id)`, `list()`, `update_artifact(id, name, text)`, `upload(id, name, data)`, `run(id, from_step=None, only=None)` (409 если уже идёт), `cancel(id)`.
- `STEPS = ("text", "poster", "plan", "images", "voice", "render", "short", "publish")`.
- `studio.llm.complete(prompt: str, *, cmd, cancel) -> str`; `extract_json(text) -> object`.
- `studio.slides.Slide(para: int, title: str, bullets: list[str], image_prompt: str)`; `parse_plan(obj, paragraphs: int) -> list[Slide]` (зажимает `para`, сортирует, первый слайд с 0); `timings(paragraphs: list[str], slides, total_s, intro_s) -> list[float]` (длительности).
- `studio.assemble.render_slide(spec, out_mp4, *, ffmpeg, font, size, seconds, image=None)`; `concat(parts, audio, out, size)`; `ORIENT = {"landscape": (1920,1080), "vertical": (1080,1920)}`.
- Настройки сценария: `poster_mode: "local"|"upload"`, `draw_images: bool`, `voice_mode: "tts"|"upload"`, `engine`, `voice`, `shorts: bool`, `review_text: bool`, `channels: list[str]`.

- [ ] Тесты: `parse_plan` (битый JSON → `StudioError`, para вне диапазона зажимается, пусто → один слайд на весь текст); `timings` (сумма = длина аудио − интро, минимум 1,5 с); `render_slide` строит фильтр с `zoompan`/`drawtext` и экранирует `:` `'` `%` в тексте; при наличии ffmpeg — mp4 нужного размера; прогон «статья» на фейках через HTTP до `video.mp4` и `short.mp4`; повторный run → 409; режим «загружу постер сам» → шаг `poster` в `waiting`, после загрузки продолжение.
- [ ] Коммит `feat(studio): сценарий «видео по статье» — от текста до mp4 и шортс`.

### Task 6: Сценарий «Дайджест новостей»

**Files:** `aiplatform/studio/news.py`, `aiplatform/fakes/fake_news.py`, правки `steps.py`, `static/studio/*`, `tests/test_studio_news.py`.

**Produces:** `news.collect(day: date, *, collect_cmd: list[str], where_cmd: list[str], cancel, on_line) -> Path` — каталог выпуска с `narration.md` (+`post.md`); шаг `text` у сценария `digest` копирует их в проект.

- [ ] Тесты: разбор вывода `where` (строка «Озвучить: <path>/narration.md → …»), «Дайджеста за … нет» → ошибка шага, день в будущем → 400, прогон дайджеста на фейках до видео.
- [ ] Коммит `feat(studio): сценарий «дайджест новостей» через narrator`.

### Task 7: Публикация

**Files:** `aiplatform/studio/publish.py`, `app/platform/channels.example.json`, правки `steps.py`/UI, `tests/test_studio_publish.py`.

**Produces:** `publish.load_channels(path) -> list[Channel(id,label,type,format,chat_id,token_env)]`; `publish.publish(channel, project_dir, *, video, short, post_text, http=urllib) -> dict(result)`.

- [ ] Тесты: manual → пакет с mp4/title/description; telegram с подменённым HTTP → multipart с `chat_id`, `caption` ≤1024, `parse_mode=HTML`; нет токена → ошибка без значения токена; vertical-канал без шортса → ошибка «включи шортс».
- [ ] Коммит `feat(studio): публикация в каналы`.

### Task 8: Документация и финал

- [ ] `app/platform/README.md`: запуск, переменные, установка моделей (ссылки на исходные репо), сценарии; корневой `README.md`.
- [ ] Статусы: план → `завершено`, спека → `реализовано`; `./check`; `./finish`.
