# Матрица приёмки MASTER_SPEC (PRD-027)

Каждое требование ТЗ → где в коде → чем проверено → статус. Статусы:
**реализовано** · **частично** (что именно не вошло) · **недоступно в окружении**
(код есть, на этой машине нет инструмента — честный `unavailable`) ·
**не реализовано** (решение ADR 0011). Проекция ТЗ на стек — ADR 0011,
аудит машины — `ENVIRONMENT_AUDIT.md`. Команды проверки — внизу.

Код — `app/platform/aiplatform/montage/`, тесты — `app/platform/tests/`.

## §1 Продукт и сценарии

| Требование | Код | Тест | Статус |
|---|---|---|---|
| Проект, 1+ видео, аудио, материалы PDF/MD/TXT/DOCX | `projects.py` (`save_source`), `documents.py` | `test_montage_story.DocumentsTest`, `StoryApiTest` | реализовано; PDF — через `pdftotext` (на машине его нет → `unavailable`); отдельные аудиофайлы — не реализовано (звук берётся из «головы») |
| Ссылки как источники | — | — | не реализовано (сети нет, ADR 0011) |
| Брендбук, логотипы, референсы | `style.py` (`logo.usage`) | — | частично: стиль-библия с палитрой/шрифтом/плотностью, файлов логотипов нет |
| Стиль, длительность, аспект, язык, уровень автоматизации, бюджет, интенсивность графики | `options.mode`, `style.density`, `exports.PRESETS` (16:9, 9:16) | `test_montage_spec.ModesApiTest` | частично: целевая длительность и бюджет GPU не вводятся; аспект 1:1 нет |
| Analyze → транскрипт, удаления, структура, визуальный план | `prepare`, `story` | `StoryApiTest` | реализовано |
| Утвердить/изменить каждую стадию, auto-approve с политикой риска | ворота `edl/storyboard/export`, `autonomous` | `ModesApiTest`, `test_montage_qa.QaExportApiTest` | реализовано; политика риска = порог score и `factual_risk` без claim, не настраивается в UI |
| Preview, таймлайн, MP4, субтитры, проект, provenance | `timeline.py`, `exports.py` | `QaExportApiTest`, `test_montage_e2e` | реализовано |
| Возобновление после перезапуска, пересчёт только изменившегося | `_load` → `interrupted`; кэш сегментов и сцен по хэшам | `ModesApiTest.test_old_project_file_is_migrated`, `StoryApiTest`, `EndToEndTest` | частично: прерванное действие не продолжается само — человек нажимает повтор, кэш делает его дешёвым |
| Режимы reviewed / autonomous / manual | `MODES`, `_run` | `ModesApiTest` | реализовано |
| Offline, неизменные исходники, воспроизводимость, атомарность, аудит, клавиатура, RU/EN | `atomic_write`, `decisions.jsonl`, `source_manifest.json` (sha256), кнопки/ссылки без мыши | `test_montage_spec.DecisionsTest`, `test_atomic_writes` | реализовано; UI-строки только RU (ADR 0011) |
| Переменный FPS, 44.1/48 кГц | сегменты `fps=30`, `aresample=48000` | `test_montage.RenderArgvTest` | частично: VFR-фикстуры в тестах нет |
| Без GPU — деградация | ASR под GPU-замком, остальное CPU | — | реализовано (ffmpeg и HyperFrames — CPU) |

## §2 Архитектура

| Требование | Решение | Статус |
|---|---|---|
| Next.js / FastAPI / SQLAlchemy / Remotion / durable queue / WebSocket | stdlib HTTP + ванильный JS, файлы, HyperFrames, воркер модуля, опрос | не реализовано буквально — ADR 0011 |
| Контент-адресуемый кэш по хэшу входов и версии | `render.SegmentSpec.key`, `storyboard.scene_hash` (TEMPLATE_VERSION, стиль) | реализовано |
| Пути в пределах проекта, нет произвольных путей из браузера | `scene_file`, `export_file`, `FILES` — только известные имена | реализовано |

## §3 Реестр возможностей

| Требование | Код | Тест | Статус |
|---|---|---|---|
| Probe и запись версий; статусы | `capabilities.py` (`registry`, `probe`), `/api/montage/capabilities` | `CapabilitiesTest`, `NoModelsTest` | реализовано для ASR, LLM, HyperFrames, ffmpeg (+фильтры), Blender, pdftotext, DOCX |
| VLM, embeddings, диаризация, TTS, музыка, детекция | — | — | не реализовано (нет локальных адаптеров; TTS есть во вкладке «Аудио») |
| Адаптеры с `probe/capabilities/estimate_cost/run/validate/health` | CLI-подпроцессы (`runner.run`), `scenes.validate` | — | частично: нет `estimate_cost` |
| Маршрутизация, лог выбора, fallback, blocked с объяснением | `capabilities.route`, сцены `blocked` | `CapabilitiesTest`, `StoryApiTest` (без HyperFrames) | реализовано (детерминированный маршрут, без ручного override) |
| Секреты через env, не в репо | `AIP_*`, `secret_scan` | `./check` | реализовано |

## §4 Время

| Требование | Код | Тест | Статус |
|---|---|---|---|
| Целые кадры результата, PTS исходника, TimeMap, слова через карту | `timemap.py`, `TIMEBASE.md` | `TimeMapTest` | реализовано |
| Инварианты, пересчёт якорей после правок | `TimeMap.check`, `storyboard.reanchor`, `_write_edl` после кругов проверки | `TimeMapTest`, `StoryboardTest` | реализовано |
| Speed changes, crossfades | поля `speed`, `transition` в схеме | — | не реализовано (только `cut`, speed 1) |

## §5 Стадии

| Стадия | Код | Тест | Статус |
|---|---|---|---|
| 5.1 Ingest: checksum, probe, прокси, миниатюры, кадры/лица/сцены | `save_source`, `_source_manifest` | `ModesApiTest` | частично: sha256 и probe есть; прокси, контактные листы, лица — нет (исходник отдаётся range-запросом) |
| 5.2 VAD, ASR со временем слов, классификация, EDL с причинами и уверенностью, границы в тишине, сравнение, проверка | PRD-020/021: `asr.py`, `edl.py` (`decisions_of`), `plan.py`, круги проверки | `test_montage`, `test_montage_asr` | реализовано; диаризация — нет; «не резать согласные» — границы в тишине |
| 5.3 Фрагменты с адресом, KnowledgeMap, биты, противоречия, темп | `documents.py`, `knowledge.py`, `narrative.py` | `KnowledgeAndBeatsTest`, `StoryApiTest` | реализовано; embeddings — нет |
| 5.4 StyleBible, выбор визуала с оценкой нужности, роли, раскадровка с утверждением | `style.py`, `storyboard.py` | `StyleTest`, `StoryboardTest` | реализовано; роли «арт-директор/режиссёр/QA» — промпты одной LLM, не отдельные вызовы |
| 5.5 ≥12 шаблонов с типизированными props, детерминизм, thumbnail-тест | `templates.py`, `contracts/montage/templates/` | `TemplatesTest` (14 шаблонов на фейк-рендере, первый кадр через ffmpeg) | реализовано |
| 5.5 Blender 3D, генеративный B-roll, SVG-ассеты с provenance | `capabilities.route("scene_3d")` → blocked | `CapabilitiesTest` | не реализовано (ADR 0011; картинки/видео — вкладки «Фото»/«Видео») |
| 5.6 Спека сцены, превью/финал, альфа, рендер по хэшу, проверка ассета, логи | `scenes.py` (`render_all`, `validate`), `thumb.png` | `StoryApiTest` (кэш, правка одной сцены) | реализовано; превью в пониженном разрешении — миниатюра первого кадра |
| 5.7 Таймлайн-манифест, финал из него, субтитры SRT/VTT/в кадре, loudnorm, пик | `timeline.py`, `render.vtt`, `qa.mechanical` | `TimelineTest`, `QaExportApiTest` | реализовано; drag-and-drop, undo/redo, музыка/дакинг — не реализовано (ADR 0011; правки через сцены и ревизии) |
| 5.7 Экспорт MP4 H.264/AAC, ProRes, архив проекта | `exports.py` | `ExportsAndRevisionsTest`, `EndToEndTest` | реализовано; стемы, OTIO — нет |
| 5.8 Механический QA, семантический QA, qa_report, ремонт ≤2, экспорт только при pass или override с причиной | `qa.py`, `projects.export` | `QaTest`, `QaExportApiTest` | реализовано; авто-ремонт — только семантика (круги повторной расшифровки), механика — отчёт с `proposed_fix` |

## §6 Агенты и контроль

| Требование | Код | Тест | Статус |
|---|---|---|---|
| Роли как политики промптов, валидация схем, бюджеты, ворота, журнал | роли `rough_cut`, `research`, `narrative`, `art_director`, `storyboard`, `semantic_qa`, `gate`; `schemas.check` на каждой записи; `decisions.jsonl` | `DecisionsTest`, `StoryApiTest` (роли в журнале) | реализовано; токен-бюджеты — таймауты `runner.LIMITS`, без подсчёта токенов (ADR 0006) |
| Агенты не пишут состояние напрямую | ответы LLM → чистка (`clean_*`) → файлы пишет конвейер | тесты чистки | реализовано |
| DAG, состояния, инвалидация | `stages.py`, `_invalidate_after_plan`, `with_style` | `StoryApiTest` (правка плана/стиля) | реализовано; параллельных стадий нет (один воркер) |

## §7–8 Схемы и API

| Требование | Код | Тест | Статус |
|---|---|---|---|
| Pydantic + JSON Schema + TS | JSON Schema в `contracts/montage/` + `schemas.py` | `SchemasTest`, `TemplatesTest` | реализовано как JSON Schema; Pydantic/TS — нет (ADR 0011) |
| БД-сущности, миграции | файлы + `schema` версии (ADR 0009), миграция 1→2 | `ModesApiTest.test_old_project_file_is_migrated` | реализовано файлами (ADR 0007) |
| REST-эндпоинты из §8 | `/api/montage/…` (см. `api.md`): проекты, media, documents, analyze=`start`, rough-cut=`plan`, edl=`edl.json`+`approve`, storyboard, render-scenes+assemble=`render`, timeline, qa, export/exports, revisions/restore, capabilities/probe | `tools/api_routes.py --check` | реализовано; WS /events — опрос; PATCH → POST (контракт `api.md`) |

## §9 UX

| Требование | Код | Статус |
|---|---|---|
| Страницы: библиотека, загрузка, транскрипт с вырезками, нарратив, раскадровка-сетка, ассеты, таймлайн, монитор задач, стиль, возможности, QA, экспорт | `static/montage/app.js`, `storyboard.js` | реализовано в одной вкладке; «ассеты» — композиции сцен; таймлайн — просмотр дорожек |
| Per-scene regenerate / switch engine / edit prompt / visual type / disable / frame range / swap assets / update style | карточки сцен: переделать, включить, кадры, props/шаблон через API (`POST storyboard`), стиль | частично: шаблон и props правятся через API, в UI — кадры, включение, замечание |
| Честный прогресс, ошибки, причины блокировок, RU/EN | этапы PRD-025, `blocked`/`interrupted`, причины сцен | реализовано; EN-строк нет |

## §10 Безопасность и надёжность

| Требование | Код | Статус |
|---|---|---|
| Localhost, токен, CORS/CSRF, лимиты тел, MIME/сигнатуры, пути | `server.py` (SEC-*), `uploads.sniff_video`, `documents.sniff`, `MAX_BYTES` | реализовано (платформа) |
| Код агентов не выполняется на хосте; композиции без сети | шаблоны — наш HTML, LLM даёт только props; CSP в композиции | реализовано |
| Документы — недоверенный ввод | промпт `knowledge.RULES` | реализовано (на уровне промпта) |
| Лицензии ассетов, шрифтов | `capabilities` (`license`), `ENVIRONMENT_AUDIT.md` | частично |
| Crash recovery, дубли, диск | `interrupted`, `Conflict` при занятом проекте, `core.disk` | реализовано (платформа) |
| Метрики, correlation id | `events.jsonl`, `request_id` в 500 | реализовано (платформа) |
| Бэкап БД и манифестов | `make backup`, `project.zip`, ревизии | реализовано |

## §11 Тесты и ворота

| Ворота | Проверка | Результат на машине автора (2026-10-19) |
|---|---|---|
| 100% schema-valid выходов стадий | `EndToEndTest` сверяет каждый манифест со схемой | ✓ |
| E2E на фейках: проект → ASR → EDL → ≥3 сцены → сборка → QA → mp4 → экспорт → рестарт | `test_montage_e2e.EndToEndTest` | ✓ |
| E2E без моделей: blocked/unavailable, нет подделок | `test_montage_e2e.NoModelsTest`, `StoryApiTest` (без HyperFrames) | ✓ |
| 16:9 и 9:16 | `EndToEndTest` (пресеты) | ✓ |
| RU/EN субтитры, длинные термины | `EndToEndTest.test_captions_ru_en_long_terms` | ✓ |
| Тишина, границы слов | `test_montage.EdlTest` | ✓ |
| VFR | — | не проверено (нет фикстуры) |
| Прерывание/возобновление | `test_old_project_file_is_migrated` (interrupted), кэш | ✓ (возобновление — повтором действия) |
| Перерендер только после локальной правки | `StoryApiTest`, `EndToEndTest` | ✓ |
| Длительность в кадр от манифеста (+ документированный хвост) | `EndToEndTest`, `qa.mechanical` | ✓ (QA поймал и исправил рассинхрон карты, `TIMEBASE.md`) |
| Проект переоткрывается с таймлайном и историей | `EndToEndTest`, `QaExportApiTest` | ✓ |
| 3 разных сцены глазами, читаемый текст, стиль | только фейк-рендер HyperFrames (плашка с текстом) | не проверено: HyperFrames на машине не установлен (`make models M=hyperframes`) |
| Бенчмарки на настоящем железе | — | не записаны: ASR-модели установлены, HyperFrames нет; `make asr-compare` для ASR |

## §12–13 Фазы и поставка

| Фаза / артефакт | Где |
|---|---|
| P0 аудит, решения, план | `ENVIRONMENT_AUDIT.md`, ADR 0011, спека и план `superpowers/*2026-10-19-montage-master-spec*` |
| P1–P4 | PRD-020…025 + подзадачи 1–3 PRD-027 |
| P5 роли, DAG, реестр, режимы, кэш | подзадачи 1–2 |
| P6 генеративные и 3D адаптеры | не реализовано (ADR 0011) |
| P7 QA, ремонт, безопасность, E2E | подзадачи 3, 5 |
| P8 UI, документация | подзадача 4; `user-guide.md`, `api.md` |
| README с моделями, GPU, troubleshooting | `app/platform/README.md`, `configuration.md`, `runbook.md` |
| ARCHITECTURE / AGENTS / MODEL_ADAPTERS / TIMEBASE / QA / SECURITY / API | `context.md`, этот файл + спека (роли), `capabilities.py` + `ENVIRONMENT_AUDIT.md`, `TIMEBASE.md`, `qa.py` + §5.8 выше, `configuration.md` (доступ), `api.md` |
| Пример проекта, ≥3 сцены, видео, QA-отчёт | на фейках создаёт `EndToEndTest` (в `AIP_DATA_DIR` тестов); настоящий пример — после `make models M=hyperframes` |

## Команды

```sh
./check                                   # все гейты и тесты обоих сервисов
cd app/platform && make test T="-k montage"   # 90+ тестов «Монтажа», ~4 мин
cd app/platform && make test T="tests/test_montage_e2e.py"
AIP_PORT=8791 AIP_BACKEND=fake AIP_DATA_DIR=<пусто> python3 -m aiplatform.server  # вкладка на фейках
```
