# Карта проекта

Общий контекст для агента: что это, из чего состоит, где что лежит. CLAUDE.md
импортирует этот файл — он в контексте любой сессии. Читать репозиторий,
чтобы «понять проект», не нужно: начни отсюда и открывай только нужные файлы.

**Держи карту живой.** Фича добавила или убрала модуль, вкладку, сценарий,
шаг студии, ADR, сервис, команду или файл данных — правь эту карту в том же
коммите. `tools/context_gate.py` (из `./check`) не пропустит модуль, вкладку,
шаг, сценарий или ADR, которых здесь нет. Карта — указатели, не документация:
≤300 строк, подробности — ссылкой.

## Что это

Локальная AI-платформа на Mac (Apple Silicon) для одного автора-контентмейкера:
делает и публикует видео-выпуски. Фото, видео и озвучка — локальными моделями;
тексты, промпты, слайды и судья — LLM через `claude -p` (ADR 0006).
Публикация: Telegram ботом, YouTube/TikTok/VK — ручной пакет (ADR 0008).

- Живой сервис: `http://127.0.0.1:8780`, автозапуск launchd
  (`com.aiplatform.server.plist`), деплой с тега в `~/apps/aiplatform`.
- Веса моделей: `AIP_MODELS_DIR` (у человека `/Volumes/External SSD/local_models`).
- Данные: `AIP_DATA_DIR` (по умолчанию `~/Library/Application Support/aiplatform`,
  старый вариант — `app/platform/data`). Файлы, без БД (ADR 0007).

## Сервисы (монорепо)

| Каталог | Что |
|---|---|
| `app/platform/` | веб-платформа: Python stdlib HTTP-сервер (ADR 0003) + ванильный JS без сборки. Пакет `aiplatform` |
| `app/narrator/` | движок озвучки, новостей и Telegram-бота. CLI `./narrate`, пакет `narrator` (uv). Своих страниц нет; свой регламент — `app/narrator/CLAUDE.md` |
| `contracts/bridge/` | JSON-схемы ответов моста `narrate bridge <cmd>`; проверяют тесты обеих сторон (QA-004) |
| `tools/` | гейты `./check`, релиз, сдвиг staging, шаблоны `.claude/` |
| `docs/` | спеки, планы, ADR, гайды (ниже) |
| `backlog/` | задачи вне git, только в главном checkout (см. CLAUDE.md) |

## Платформа: бэкенд `app/platform/aiplatform/`

- `server.py` — `App`: собирает `Context` (Notifier, GPU `Gate`, `UploadStore`) и
  модули в порядке шапки; роутинг `/api/<prefix>/…` → `module.handle(req)`
  (`req.sub` — остаток пути, разбор через `match`). Общие ручки
  `/api/config|status|health|ready`. Статика `static/<prefix>/index.html`, `/` → `/photo/`.
  Защита: Host, Origin или `X-AIP: 1` на POST, токен `AIP_AUTH_TOKEN`, CSP.
  Протокол модуля: `prefix, label, handle, describe, problems, close`
  (+ `nav, bind, badges, ready_checks`). Вкладки шапки — `server.nav_tabs`, группы `NAV_GROUPS`.
- `config.py` — `Config` и чтение `AIP_*` (все переменные — `docs/configuration.md`).
  `AIP_BACKEND=fake` подменяет все CLI на `fakes/` — тот же путь кода.
- `contracts.py` — `make contracts`: снимки `--help` CLI генерации
  (`tests/contracts/*-help.txt`), сверка argv в `tests/test_cli_contracts.py`.
- `backup.py` — `make backup/restore`: tar.gz невосстановимых данных, шифрование age.
- `smoke.py` — короткий прогон через HTTP API (`make smoke`, `--fake`).

| Пакет | API | Что |
|---|---|---|
| `photo/` | `/api/photo/` | картинки (mflux, Flux2CLI), правка/маска/расширение, апскейл SeedVR2; `jobs.py`, `models.py` CATALOG |
| `video/` | `/api/video/` | ролики LTX и Wan, ключевые кадры (`timeline.py`), «живое фото» (`living.py`) |
| `audio/` | `/api/audio/` | текст → mp3 через narrator; движки и голоса — `voices.py` |
| `avatar/` | `/api/avatar/` | «Диктор»: персонаж по фото говорит текст — озвучка очередью «Аудио», анимация SadTalker (`sadtalker.py`), персонажи `characters.py` |
| `montage/` | `/api/montage/` | «Монтаж»: своя запись → ролик (ниже) |
| `studio/` | `/api/studio/` | сценарии выпусков и доска «Выпуски» (ниже) |
| `library/` | `/api/library/` | «Статьи»: библиотека narrator через мост, озвучка/проверка/отправка — фоновые `core.tasks` |
| `news/` | `/api/news/` | источники, главное за день, добор упавших, обзоры недели/месяца |
| `channels/` | `/api/channels/` | каналы публикации (`channels.json`, `store.py`), бот, подписчики, `narrate bot serve` |
| `updates/` | `/api/updates/` | версии моделей (PyPI/GitHub/HF), реестр запрещённых организаций и иноагентов |
| `settings/` | `/api/settings/` | `settings.json`: выключенные модели/голоса, расписание, уведомления, уборка |
| `core/` | — | общие механизмы (ниже) |
| `fakes/` | — | имитации CLI для `make demo` и тестов (`fake_narrate.py`, `fake_gen.py`, `fake_ltx.py`, `fake_sadtalker.py`, `fake_llm.py`…) |
| `static/` | — | фронтенд (ниже) |

`core/`: `gate.py` GPU-замок, одна тяжёлая модель за раз (ADR 0004) · `narrator.py`
мост к движку · `runner.py` подпроцессы, pid в `data/run/` · `tasks.py` фоновые
команды движка · `jobs.py` выборка задач · `schema.py` версия схемы, миграции,
карантин `*.bad` (ADR 0009) · `events.py` `events.jsonl` · `audit.py` комплаенс ·
`cleanup.py` уборка · `disk.py` свободное место · `uploads.py` загрузки по сигнатуре ·
`media.py` ffmpeg/ffprobe · `http.py` Request/HttpError · `settings.py` ·
`notify.py` уведомления · `log.py` · `perms.py` · `fsutil.py` atomic_write · `words.py`.

## Студия `studio/`

Сценарии (`service.py` `SCENARIOS`): `digest` — дайджест новостей (материал = день,
текст собирает narrator: `make news-collect news-agent news-judge`), `article` —
видео по статье (вкладка по умолчанию), `post` — пост в канал (видео = постер под
озвучку, подпись в `post.md`).

Шаги (`pipeline.py` `STEPS`, в скобках зависимости):
1. `text` — текст для озвучки (digest — из narrator; post — из подписи).
2. `poster_prompt` (text) — LLM пишет промпт постера.
3. `poster` (poster_prompt) — первый кадр: локальная модель или загрузка человеком (дедлайн).
4. `plan` (text) — LLM: `plan.json`, слайды и промпты картинок/роликов (`slides.py`).
5. `images` (plan) — картинки слайдов: «Фото» или загрузка.
6. `clips` (plan) — до 3 роликов: «Видео» или загрузка.
7. `voice` (text) — озвучка: «Аудио» или свой mp3.
8. `render` (poster, plan, images, clips, voice) — ffmpeg `video.mp4` 16:9 (`render.py`, `motion.py`).
9. `short` (text, poster, images, clips) — шортс 9:16.
10. `publish` (render, short) — публикация; после рестарта сама не повторяется.

- `pipeline.py` — класс `Studio`: `project.json`, граф шагов, фоновый запуск,
  Waiting/Skip/Conflict, сброс зависимых, миграция при загрузке (не трогает `updated_at`).
- `steps.py` — функции шагов, `Services` (очереди photo/audio/video), журнал `published.json`.
- `service.py` — HTTP, доска (`board_group`: attention «Ждут тебя» / running / uploaded /
  published / drafts; готовое видео без публикации — attention), счётчик шапки `badges`,
  дедуп по ключам материала, clone/split/summary, публикация с доски.
- `publish.py` — каналы `telegram` (Bot API, подпись ≤1024) и `manual` (папка-пакет);
  состояния sending/sent/unknown/failed, разрешение неясных — `publish/resolve`.
- Остальное: `llm.py`, `prompts.py`, `news.py`, `cache.py` (кэш по sha256 входов),
  `estimate.py` (оценка времени), `series.py` (статья → серия), `review.py` (ревью из
  бота владельца), `schedule.py` (дайджест по расписанию и ежедневные работы),
  `stats.py` (подписчики, просмотры, окна 24ч/72ч/7д), `metrics.py`, `history.py`
  (архив аналитики), `backfill.py`, `config.py`, `assets/`.

## Монтаж `montage/`

Своя запись («голова» + по желанию экран) → ролик для YouTube; свой список роликов, не доска.
Конвейер (`projects.py`): звук → сдвиг экрана (`sync.py`) → тишина и расшифровка (`asr.py`, воркер
`asr_worker.py` в окружении mlx-audio, `transcript.py`) → план LLM по индексам слов (`plan.py`,
правила `assets/rules.md` + выученные `lessons.md`) → человек правит → сегменты с кэшем, склейка,
финал (`edl.py`, `render.py`) → проверка повторной расшифровкой (до 2 кругов). Замечание человека →
новый план и правило в `lessons.md`. `compare.py` — `make asr-compare`.

## Фронтенд `aiplatform/static/`

Ванильный JS, ES-модули без сборки; страница вкладки = `static/<tab>/index.html` + `app.js`.
- Общее: `ui.js` (`api()` с `X-AIP`, `narratorApi`, `h()`, `poller`, `jobProgress`,
  `statusChip`/`toneOf`, `modal`, `toast`, `uploadFile`), `nav.js` (шапка из
  `/api/config` → `tabs`, GPU, бейдж `studio_attention` из `/api/status`, баннер `problems`),
  `core/format.js` (`seconds`, `minutes`, `plural`…), `core/consent.js`,
  `tokens.css` (все цвета/размеры), `ui.css` (компоненты), `common.css` (шапка, база).
- Вкладки: `photo/` (+`exif.js`, `geometry.js`), `video/` (+`frames.js`), `audio/`, `avatar/` (+`form.js`), `montage/` (+`plan.js`),
  `board/` «Выпуски», `studio/` (`state.js`, `create.js`, `steps.js`, `head.js`,
  `media.js`, `publish.js`, `settings.js`), `library/` «Статьи», `news/`,
  `channels/`, `updates/`, `settings/`.
- Тесты фронта: `app/platform/tests/js/*.test.mjs` (`node --test`, в `make lint`).

## Мост к narrator

`core/narrator.py`: home = `AIP_NARRATOR_DIR` (по умолчанию `app/narrator`),
cmd = `<home>/narrate`. Данные — `narrate bridge <cmd>` (JSON stdin/stdout,
таймаут 120 с, ошибка → `kind` invalid/not_found/unavailable → 400/404/503;
чтения повторяются раз). Долгое — `make -C <home> <target>` или `narrate …` как
`core.tasks` (ADR 0005). Версия контракта: `BRIDGE_API` в `narrator/bridge.py` ==
`EXPECTED_BRIDGE_API` в `core/narrator.py`, иначе баннер. Схемы — `contracts/bridge/`.
Пользуются audio, library, news, channels, updates.

## Движок `app/narrator/src/narrator/`

CLI `./narrate -h` (setup, voice, fetch, new, lint, synth, say, normalize, verify,
render, index, news …, bot …, bridge). Проверки — `make check` (ruff + pytest).

- `cli.py` команды · `bridge.py` JSON для платформы · `synth.py` движки, кэш блоков,
  сборка mp3, голоса-клоны `CLONE_VOICES` · `mlx_engines.py` + `mlx_worker.py` модели
  mlx-audio (higgs, fish, moss, qwen3, chatterbox) в отдельном процессе ·
  `models.py` пути к весам · `normalize.py`, `lexicon.py` текст → произношение ·
  `stress.py` ударения RUAccent · `script.py` блоки текста · `verify.py` проверка
  распознаванием (faster-whisper) · `fetch.py` скачивание статьи (trafilatura) ·
  `library.py` статьи и `meta.json` · `telegram.py` бот и каналы · `registry.py`
  реестр организаций · `consent.py` согласие владельца голоса (CMP-009) ·
  `gemini.py` образец голоса Charon через Gemini TTS · `gate.py` одна модель за раз ·
  `audit.py` комплаенс-журнал · `voices/` образцы · `news/` сбор, дайджест,
  обложки, источники, база `news.db`.
- Движки TTS: `espeech` (по умолчанию, GPU), `silero` (CPU, CC BY-NC), `supertonic`
  (CPU), mlx-движки; Higgs (MLX) — движок дайджеста. Голоса: Charon, M1, Shem, Shem-v2.
- Данные движка (вне git): `library/<год>/<мес>/<день>/<slug>/` (source.md,
  script.txt, meta.json, check.md, mp3), `library/news/news.db`,
  `library/telegram.json` (подписчики), `telegram_tems.md` (каналы), `bot_token.md`.
  Токен и каналы агент не читает.

## Данные платформы (`AIP_DATA_DIR`)

- Корень: `settings.json`, `channels.json`, `registry.json`, `backup.json`,
  `events.jsonl`, `audit.jsonl`, `notifications.log`, `logs/platform.log`, `run/`,
  `uploads/`, `analytics/publications.jsonl`.
- `photo/jobs/`, `video/jobs/`, `audio/jobs/`, `avatar/jobs/`, `library/tasks/` — задачи вкладок;
  `avatar/characters/<id>/` — персонажи диктора (картинка, согласие на лицо).
- `montage/projects/<id>/`: `project.json`, `sources/`, `audio.wav`, `words.json`, `silences.json`,
  `plan.json`, `segments/` (кэш), `output.mp4`, `subtitles.srt`; `montage/lessons.md` — выученные правила.
- `studio/projects/<id>/`: `project.json`, `text.md`, `post.md`, `poster.*`,
  `plan.json`, `images/`, `clips/`, `voice.*`, `video.mp4`, `short.mp4`,
  `published.json`, `narrator/` (review.md судьи), `publish/<канал>/`;
  `studio/cache/`, `studio/schedule.json`.

## Модели

Фото: Z-Image Turbo (по умолчанию), Krea-2, Qwen-Image-2512, Ideogram 4,
FLUX.2 Klein-9B/Dev. Видео: LTX-2.5 Q4 (по умолчанию), LTX-2.3 Q4, Wan 2.2.
Апскейл: SeedVR2. Диктор: SadTalker (CPU, окружение в `~/.local/share/avatar`).
Расшифровка монтажа: Whisper Large V3 Turbo (по умолчанию), Whisper Large V3, Qwen3-ASR 1.7B +
ForcedAligner — mlx-audio в `~/.local/share/montage`, веса в `<AIP_MODELS_DIR>/asr/`. Ставит `app/platform/tools/setup-model.sh` (`make models M=`).
Известная беда: Flux2CLI без `mlx.metallib` — `docs/грабли.md` №1.
ffmpeg — только `ffmpeg-full` (keg-only, в PATH или `AIP_FFMPEG`): формула `ffmpeg`
без freetype, а постер и слайды рисуются `drawtext`. Нет фильтра — баннер (`media.has_filter`).

## Решения (ADR, `docs/decisions/`)

0001 реестр организаций — файлом, путь из env · 0002 шаблоны `.claude/` в репо,
ставит `bootstrap.sh` · 0003 stdlib `http.server` · 0004 один GPU-замок · 0005 narrator
подпроцессом: CLI для долгого, мост `bridge` для данных · 0006 LLM через `claude -p` ·
0007 файлы вместо БД · 0008 Telegram ботом, остальное ручным пакетом · 0009 версия
схемы данных, миграции, карантин · 0010 лицензии движков озвучки и голос Charon.

## Запуск и проверки

- `app/platform`: `make run` (настоящие модели) · `make demo` (фейки, `data-demo`) ·
  `make test` (pytest-xdist; `T="-k studio"`) · `make lint` (ruff, `node --check`, JS-тесты) ·
  `make check` · `make smoke` · `make deploy TAG=` · `make backup` ·
  `make install-agent` (launchd + чекер дайджеста) · `make release-prep/release-tag`.
- Свой порт и данные для проверки UI: `AIP_PORT=8791 AIP_BACKEND=fake AIP_DATA_DIR=<копия> python3 -m aiplatform.server`.
- Корень: `./check` — secret_scan, docs_gate, readme_gate, api_routes `--check`,
  tasks_gate, context_gate, тесты тулинга, `make check` каждого `app/<svc>/`.
  `./finish` — check → push ветки → fast-forward `staging` → PR `staging → main`.
- Тесты бэкенда: `app/platform/tests/` (`helpers.py`: `PlatformCase`, `StudioCase` на фейках).

## Где что искать

| Нужно | Где |
|---|---|
| как пользоваться вкладками | `docs/user-guide.md` |
| переменные `AIP_*`, автозапуск, деплой, бэкап | `docs/configuration.md` |
| HTTP API: контракт / все маршруты | `docs/api.md` / `docs/api-routes.md` (собирает `tools/api_routes.py`) |
| что делать при сбое живого сервиса | `docs/runbook.md` |
| установка и разработка платформы | `app/platform/README.md` (≤200 строк) |
| спеки и планы | `docs/superpowers/` (главная — `specs/2026-10-15-ai-platform-design.md`) |
| релизы | `docs/CHANGELOG.md` |
| прошлые системные ошибки | `docs/грабли.md` |
| что уже сделано / открыто | `backlog/CHANGELOG.md`, `backlog/README.md` |
