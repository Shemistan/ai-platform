---
тип: spec
статус: утверждено
обновлено: 2026-10-19
план: 2026-10-19-montage-master-spec.md
---

# «Монтаж» по MASTER_SPEC: от записи до смонтированного ролика с раскадровкой, QA и проектом (PRD-027)

## Зачем

`MASTER_SPEC.md` описывает студию видеопродакшена. Платформа уже умеет
большую часть (PRD-020…026); ADR 0011 закрепляет, как ТЗ ложится на стек
платформы. Эта спека — что именно добавляется к «Монтажу» и как оно устроено.

Успех: автор загружает запись и материалы (конспект, статью), выбирает
режим, получает смонтированный ролик с моушн-сценами по раскадровке, которую
он утвердил, QA-отчёт, экспорт с provenance и проект, который переоткрывается
после рестарта с историей ревизий. Без LLM или HyperFrames — честные
`blocked`/`unavailable` вместо поддельных артефактов.

## Что есть и что добавляется

Есть: загрузка «голова»/«экран», звук, сдвиг, тишина, расшифровка (слова со
временем), план вырезок LLM, подтверждение в тексте, сборка по сегментам с
кэшем, loudnorm, SRT/ASS, проверка повторной расшифровкой (≤2 кругов),
моушн-вставки (5 шаблонов), этапы с прогрессом, уроки.

Добавляется (разделы ТЗ в скобках):

1. **Режимы и ворота** (§1, §6): `mode` ∈ `reviewed | autonomous | manual`.
   Ворота: `edl` (план вырезок), `storyboard`, `export`. В `reviewed` каждое
   ждёт человека; в `autonomous` ворота проходятся сами с записью в журнале
   решений («autonomous mode»), QA обязателен; в `manual` план вырезок LLM не
   строится (только правки человека), раскадровка — по кнопке.
   Поле `approvals: {edl: {at, by, auto}, storyboard: …, export: …}`.
   Старая галочка `auto` = `mode: autonomous`.
2. **Схемы** (§7): JSON Schema в `contracts/montage/*.schema.json`
   (`project`, `source_media`, `source_document`, `transcript_word`,
   `edit_decision`, `time_map_segment`, `knowledge_claim`, `narrative_beat`,
   `style_bible`, `storyboard_scene`, `motion_spec`, `timeline`, `qa_item`,
   `export_preset`, `capability`, `revision`). Проверка — `montage/schemas.py`
   (`validate(name, value) -> list[str]`), зовётся при записи каждого файла
   конвейера и в тестах; `schema_version` в каждом файле.
3. **TimeMap** (§4): `montage/timemap.py`. Целые кадры при 30 fps результата
   (`render.FPS`); сегмент = `{source_id, source_pts_start, source_pts_end,
   edit_frame_start, edit_frame_end, speed: 1, transition: "cut"}`.
   Строится из `edl.Edl`; функции `to_edit_frame(source_s)`,
   `to_source(edit_frame)`, `words_in_edit(words)`, `check()` (нет
   отрицательных, нет пересечений, в границах исходника, сумма кадров =
   длине результата). Файл `timemap.json`. Подробности — `docs/TIMEBASE.md`.
4. **Журнал решений и provenance** (§6, §10): `decisions.jsonl` в проекте —
   каждая запись: `{at, role, action, model, prompt_version (sha256 правил +
   промпта)[:12], input_hash, output_hash, confidence, rationale, ids}`.
   Пишут: план, замечание, проверка, знания, биты, стиль, раскадровка,
   ворота. `source_manifest.json` — sha256 и probe каждой записи и
   документа.
5. **Реестр возможностей** (§3): `montage/capabilities.py`. Записи:
   `asr:<id>` (установлена/выключена), `llm`, `hyperframes`, `ffmpeg`
   (+ фильтры `subtitles`, `drawtext`, `loudnorm`, `blackdetect`, `astats`),
   `blender`, `pdftotext`, `docx` (stdlib — всегда). Статусы
   `available | unavailable | disabled`, `probe()` запускает настоящую
   команду (`--version`/`-h`) и пишет `probed_at`, `version`, `detail`.
   `route(task)`: `scene_render → hyperframes | blocked`, `scene_3d →
   blocked`, `document_pdf → pdftotext | unavailable`. `GET/POST
   /api/montage/capabilities[/probe]`.
6. **Материалы и знания** (§5.3): `montage/documents.py` — загрузка
   `.md .txt .docx .pdf` (pdf — только с pdftotext; docx — zip+xml stdlib) в
   `documents/<id>.<ext>` + `documents.json` (id, name, sha256, chars, pages,
   status). Текст режется на фрагменты ~800 знаков с номером страницы/раздела
   (`chunks.json`). `montage/knowledge.py` — LLM-задача `montage_knowledge`:
   `{claims: [{statement, citations: [{doc, chunk, quote}], transcript:
   [from, to], confidence, contradiction_status: none|contradicted|unsupported}]}`
   → `knowledge.json`. Без документов — claims только из расшифровки
   (`citations` пустые, `unsupported` не ставится). Документы — недоверенный
   ввод: промпт говорит игнорировать инструкции внутри них.
7. **Нарративные биты** (§5.3): `montage/narrative.py` — задача
   `montage_beats`: `{beats: [{from, to, purpose: hook|setup|explanation|
   demo|recap|cta|other, goal, difficulty: 1–5, keywords, visuals_needed,
   claims, priority: 1–5, confidence}]}` по индексам слов результата
   (после EDL) → `beats.json` с `start_frame/end_frame` из TimeMap.
   `pacing`: длительность битов, доля «explanation» подряд.
8. **Стиль-библия** (§5.4): `montage/style.py` — `StyleBible`: palette (как
   `motion.agents.PALETTE_KEYS`), typography (font/mono из `agents.FONTS`),
   spacing, motion curves (`enter/exit/hold` easing), caption style, safe
   margins, logo usage, density (`low|medium|high`). По умолчанию — из
   дизайн-системы (`DEFAULT`); LLM-задача `montage_style` уточняет под тему.
   Версии `style_001…` в `styles/`, активная — `style_bible.json`.
9. **Раскадровка** (§5.4, §7): `montage/storyboard.py` — задача
   `montage_storyboard` по битам: для каждого бита `visual_type` ∈ `presenter
   | callout | diagram | code | chart | list | comparison | timeline | title |
   kinetic` и шаблон из каталога; `necessity: {clarity, novelty,
   cognitive_load, production_cost, factual_risk}` (1–5) → `score`;
   сцена ставится, если score ≥ порога и не теснее `MIN_GAP_S` к соседям; не
   «каждые N секунд». Схема `StoryboardScene` как в ТЗ (`id`, `beat_id`,
   `edit_start_frame`, `duration_frames`, `fps`, `intent`,
   `grounding_claim_ids`, `visual_type`, `engine`, `template_id`,
   `style_bible_revision`, `props`, `animation`, `layers`, `status:
   proposed|approved|rendered|blocked|disabled|failed`, `rationale`,
   `necessity`, `hash`). Файл `storyboard.json`. Правки человека: включить/
   выключить, диапазон кадров, шаблон, props, «переделать с замечанием»
   (одна сцена → LLM). Сцена с `factual_risk ≥ 4` без claim → не ставится
   (§5.3: не утверждать картинкой то, чего нет в источниках).
10. **Каталог шаблонов** (§5.5): `montage/templates.py` — 12 шаблонов
    HyperFrames: `kinetic_typography`, `lower_third`, `chapter_title`,
    `concept_cards`, `flow_diagram`, `comparison`, `timeline`, `step_list`,
    `code_highlight`, `task_scheduler`, `data_chart`, `callout_zoom`
    (+ старые `stat`, `quote` как алиасы `callout_zoom`/`kinetic`). У каждого:
    `props` со схемой (`contracts/montage/templates/<id>.schema.json`),
    ограничения (длины, число элементов, минимальный кегль), `html(props,
    style)`, `anim(props, duration)`, детерминизм (ни `Math.random`, ни
    времени вне таймлайна). Композиция — как `motion.composition`, со
    стилем из стиль-библии (`css_vars`). Тест: каждый шаблон собирается с
    примером props, проходит `agents.clean_code`-подобную проверку запретов и
    рендерится фейком в MOV нужной длины; «thumbnail test» — первый кадр
    через ffmpeg.
11. **Рендер сцен** (§5.6): `montage/scenes.py` — `scenes/<scene_id>/`:
    `hash = sha256(template, props, style revision, duration_frames, fps,
    TEMPLATE_VERSION)[:16]`; `index.html`, `<hash>.mov`, `thumb.png`
    (первый кадр), `scene.json`. Рендер только если `<hash>.mov` нет;
    проверка результата: длительность = кадры/fps ± 1 кадр, `pix_fmt` с
    альфой, размер 1920×1080. Нет HyperFrames — сцены `blocked` с
    `INSTALL_HINT`, финал собирается без них, QA пишет `warning`.
12. **Таймлайн-манифест** (§5.7): `montage/timeline.py` — `timeline.json`:
    `tracks: [{id: presenter|screen|overlays|captions|dialogue, kind,
    clips: [{id, source_id, source_in, source_out, edit_in_frame,
    edit_out_frame, layout, scene_id?, enabled, gain?}]}]`, строится из
    EDL + раскадровки + субтитров; финал (`_final`) берёт наложения **из
    таймлайна**, не из раскадровки напрямую, — превью и финал от одного
    манифеста. Правка клипов — через правку сцен (диапазон кадров, enabled);
    drag-and-drop не входит.
13. **Субтитры** (§5.7): к SRT добавляется VTT (`subtitles.vtt`), кегль и
    поля из стиль-библии; переполнение строки (> 42 знаков) — QA warning.
14. **QA-отчёт** (§5.8): `montage/qa.py` → `qa_report.json`:
    `{schema_version, made_at, items: [QAItem], gates: {mechanical: pass|fail,
    semantic: pass|fail|human_review}, repair_attempts}`. QAItem:
    `{id, kind: mechanical|semantic, check, severity: error|warning|info,
    message, evidence: {t_start, t_end, frame?, scene_id?}, proposed_fix,
    attempts, status: open|fixed|escalated|accepted}`. Механика: длительность
    результата vs сумма сегментов (±1 кадр, допуск loudnorm задокументирован),
    есть видео и звук, 1920×1080 и 30 fps, чёрные кадры (`blackdetect`),
    тишина дольше `max_pause + 0.3`, клиппинг и пик (`astats` → `Peak level`),
    сцены вне таймлайна или без файла, субтитры длиннее 42 знаков, нет
    шрифта для drawtext. Семантика: существующая повторная расшифровка + LLM
    (`montage_check`) — без LLM `semantic: human_review`. Ремонт: круги
    проверки (≤2) = `attempts`; после — `escalated`.
15. **Экспорт** (§5.7, §5.8): `montage/exports.py` — пресеты
    `youtube_1080p` (H.264/AAC, 16:9, по умолчанию), `vertical_1080x1920`
    (центр-кроп 9:16 с головой), `prores_master` (ProRes 422 HQ, если ffmpeg
    умеет). `POST export {preset, override_reason?}`: если
    `gates.mechanical == fail` и нет причины → 409; с причиной — запись в
    журнал. Результат `exports/<n>/`: `export.mp4|mov`, `provenance.json`
    (источники, модели, версии промптов, хэши сцен, qa gates, override),
    `project.zip` (манифесты + субтитры, без медиа). `GET exports`.
16. **Ревизии** (§8): `montage/revisions.py` — `revisions/<n>.json` —
    снимок манифестов (project.json без log, plan, edl, timemap, beats,
    knowledge, style, storyboard, timeline) с `label` и `at`; снимок после
    каждого действия и ручной правки; `POST revisions/<n>/restore`
    восстанавливает манифесты (медиа и кэш не трогает) и делает новый снимок
    «restore». Переоткрытие после рестарта — из файлов (уже так).
17. **Инвалидация** (§6): правка плана вырезок → сброс `timemap`, `beats`,
    `storyboard` (сцены `proposed` заново; рендеры остаются в кэше по хэшу);
    правка стиля → сцены получают новую `style_bible_revision` → новый хэш →
    перерендер; правка громкости/субтитров → только финальный проход.
    Тест: правка одной сцены не меняет `<hash>.mov` других.
18. **Состояния** (§6): проект: `draft | queued | running | review |
    waiting_approval | blocked | done | failed | canceled | interrupted`
    (`review` остаётся синонимом `waiting_approval` для EDL — UI и тесты);
    сцена: `proposed | approved | rendered | blocked | failed | disabled`.
    После рестарта активное действие → `interrupted` с подсказкой (сейчас
    `failed`).
19. **UI** (§9): во вкладке «Монтаж» — выбор режима и материалы в настройках;
    после плана — кнопка «Раскадровка» (знания → биты → стиль → раскадровка);
    блоки «Нарратив» (биты списком), «Раскадровка» (сетка карточек:
    миниатюра, шаблон, кадры, score, rationale, включить/выключить, править
    props, переделать, утвердить всё), «Таймлайн» (дорожки полосами по
    кадрам, клик — сцена), «QA» (отчёт по severity, ворота), «Экспорт»
    (пресеты, override с причиной, список экспортов), «Ревизии» (список,
    восстановить), «Возможности» (реестр, probe). Клавиатура: все действия —
    кнопки и ссылки, фокус виден (ui.css).
20. **Тесты** (§11): юниты — timemap, схемы, шаблоны (12), раскадровка
    (score, spacing, factual_risk), QA, ревизии, инвалидация; E2E на фейках:
    проект → загрузка → ASR → утверждение EDL → раскадровка ≥3 разных
    шаблонов → сборка → QA → проигрываемый mp4 (ffprobe) → экспорт →
    рестарт сервера → проект на месте с ревизиями; E2E без LLM и HyperFrames
    (`AIP_LLM_CMD=/nonexistent`, `AIP_MONTAGE_HYPERFRAMES_CMD=/nonexistent`
    на `AIP_BACKEND=` реальном) — `blocked`/`unavailable`, файлов сцен нет.
    Матрица — `docs/ACCEPTANCE.md`.

## Конвейер

```
prepare: звук → сдвиг → тишина → ASR → source_manifest → план (кроме manual) → ворота edl
story:   timemap → материалы (chunks) → знания → биты → стиль → раскадровка → ворота storyboard
render:  сегменты (кэш) → склейка → сцены (кэш по хэшу) → таймлайн → финал (loudnorm, субтитры, наложения)
         → QA (механика + повторная ASR/LLM, ≤2 кругов ремонта) → qa_report → ворота export
export:  пресет → exports/<n>/ + provenance + project.zip
```

`autonomous`: prepare → story → render подряд, ворота проходятся сами; экспорт
по умолчанию `youtube_1080p`, если `gates.mechanical == pass`.

## Данные проекта (добавка к PRD-021)

`source_manifest.json`, `documents/`, `documents.json`, `chunks.json`,
`edl.json`, `timemap.json`, `knowledge.json`, `beats.json`, `styles/`,
`style_bible.json`, `storyboard.json`, `scenes/<id>/`, `timeline.json`,
`subtitles.vtt`, `qa_report.json`, `exports/<n>/`, `revisions/<n>.json`,
`decisions.jsonl`.

## Не цели

PostgreSQL, многопользовательность, ссылки как источники, drag-and-drop
таймлайн, музыка/дакинг, диаризация, генеративный B-roll в раскадровке, 3D-шаблоны.

## Что решает строитель

Формулировки промптов, пороги score и spacing, вёрстку карточек,
точные фильтры QA, названия этапов.
