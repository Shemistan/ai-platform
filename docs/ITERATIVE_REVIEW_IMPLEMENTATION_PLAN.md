# Iterative Review & Refinement + Observability: план реализации (PRD-028)

Проекция `ITERATIVE_REVIEW_SPEC.md` на платформу — ADR 0012. Дизайн —
`superpowers/specs/2026-10-19-iterative-review-design.md`, подзадачи —
`superpowers/plans/2026-10-19-iterative-review.md`, матрица приёмки — `ACCEPTANCE.md`
(раздел «ITERATIVE_REVIEW_SPEC»).

## 1. Аудит и gap analysis (R0)

Что уже есть (PRD-020…027) и переиспользуется:

| Нужно ТЗ | Есть | Переиспользуем как |
|---|---|---|
| Baseline V0, версии, diff, восстановление | `revisions.py` — снимки манифестов без медиа; кэш сегментов и сцен по хэшам | `review/versions.py` хранит `V<n>/` с `output.mp4` (жёсткая ссылка) и манифестами; откат = манифесты + файл |
| Технический QA кандидата | `qa.py` (`mechanical`, `report`) | роль `technical_qa` и hard gates сравнения |
| Review bundle: транскрипт с кадрами, storyboard, style, manifest, QA, история | `transcript_edit.json`, `storyboard.json`, `style_bible.json`, `timeline.json`, `qa_report.json`, `decisions.jsonl`, `history` | `review/bundle.py` собирает текст и файлы; добавляет прокси, кадры, аудио-замеры |
| Привязка к стабильным id и TimeMap | `scene_id`, `beat_id`, `anchor_word`, `timemap.py` | диапазоны замечаний — кадры результата текущей версии + `scene_ids` |
| Пересборка только изменившегося | `storyboard.edit` → новый хэш одной сцены; `scenes.render_all` кэш; сегменты по `SegmentSpec.key` | `review/repair.py` правит манифесты и зовёт `_render` |
| Ворота и режимы | `approvals`, `MODES` | `review_policy.mode` отдельный: `manual_approval / auto_safe / autonomous` |
| Этапы и прогресс | `stages.py`, `project.stages`, ffmpeg `-progress` | `progress.py` — события поверх того же трекера |
| Журнал решений | `decisions.jsonl` | решения арбитра, приём/откат версии, выбор ревизии |
| LLM-роли, фейки | `fake_llm.py` по `### task:` | задачи `montage_review_<role>`, `montage_review_judge` |

Чего нет (делается):

- запусков/раундов/замечаний/плана/сравнения как сущностей и их API;
- извлечения прокси, кадров, аудио-замеров (`ffmpeg`), replay-кадров для UI;
- ролей-ревьюеров (10), валидатора, арбитра, планировщика, судьи;
- state machine с чекпоинтами, pause/resume/stop, бюджетов, early stop, защиты от осцилляции;
- событий с курсором, дерева прогресса, ETA по истории машины, SSE, консоли логов, ресурсов, истории запусков;
- UI: панели ревью и прогресса;
- E2E ТЗ §12 и §14.8.

Чего на машине нет и честно не делается: мультимодальная модель (роли «глаз»
деградируют), GPU-метрики (нет API без root), Remotion/Blender (адаптеры не нужны).

## 2. «Миграции» (файлы вместо таблиц)

Новые файлы в `montage/projects/<id>/` (схемы — `contracts/montage/`):

| Таблица ТЗ | Файл | Схема |
|---|---|---|
| `review_runs` | `review/runs/<run_id>/run.json` | `review_run` |
| `review_rounds` | `review/runs/<run_id>/rounds/<n>/round.json` | `review_round` |
| `reviewer_reports` | `…/rounds/<n>/reports/<role>.json` | `reviewer_report` |
| `review_issues`, `review_issue_evidence` | `…/rounds/<n>/issues.json` → `issues[]` (evidence внутри) | `review_issue` |
| `repair_plans`, `repair_actions` | `…/rounds/<n>/repair_plan.json` → `actions[]` | `repair_plan`, `repair_action` |
| `review_comparisons` | `…/rounds/<n>/comparison.json` | `review_comparison` |
| `review_decisions` | `decisions.jsonl` (роли `arbiter`, `human`, `regression_judge`) + `issues[].decisions[]` | `decision` (как в PRD-027) |
| `review_policies` | `run.json` → `policy` | `review_policy` |
| `revision_lineage` | `review/versions/V<n>/version.json` (`parent`, `round`, `accepted`) | `review_version` |
| `run_events` | `events.jsonl` (`seq` — курсор, `event_id` = `<pid>-<seq>`) | `run_event` |
| `task_progress_snapshots` | `progress.json` (снимок дерева после каждой стадии) | — |
| `resource_samples` | `resources.jsonl` (ограничен по числу строк) | — |
| `log_artifacts` | `project.log` (хвост) + события `log`; экспорт `logs/export` | — |

Схема проекта: `options` получает `caption_words` (1–3) — версия проекта не
меняется (поле необязательное, по умолчанию 3). Исходные медиа и V0 не правятся.

## 3. Зависимости

ffmpeg-full (прокси, кадры, `signalstats`, `ebur128`, `astats`), `claude -p` для
LLM-ролей и судьи (без него — измерительные роли, субъективная часть
`human_review`), HyperFrames для перерисовки сцен (без него сцены `blocked`,
правки сцен не исполняются — план помечает их `blocked`). Ничего нового в
зависимостях Python/JS.

## 4. Риски и предохранители

| Риск | Предохранитель |
|---|---|
| Выдуманный визуальный анализ | роли «глаз» без VLM: `degraded`, `manual_check`, `evidence_type: heuristic`, уверенность ≤ 0,6; такие замечания не запускают перерисовку сами (только через утверждение) |
| Осцилляция (раунды туда-обратно по одной сцене) | отпечаток действия (`scene_id`+`op`); обратное действие по той же сцене в следующем раунде → отклоняется арбитром, `stop_reason: oscillation` при двух подряд |
| Бесконечный цикл | `max_rounds ≤ 5`, бюджеты (`max_wall_time_minutes`, `max_gpu_minutes`, `max_new_assets`), early stop при повторении замечаний |
| Порча baseline | `versions.py` не перезаписывает существующую `V<n>`; исходники не трогаются (проверка sha256 из `source_manifest.json` в hard gates) |
| Устаревшая ревизия | `run.lock.project_updated_at`; правка проекта вне запуска → `stale_revision` 409, запуск `blocked` с причиной |
| Prompt injection из транскрипта/материалов | текст идёт в промпт как данные в блоках «Расшифровка/Материалы», ответ — только JSON по схеме, валидатор отбрасывает лишнее; LLM без инструментов (SEC-004) |
| Фальшивые 100 % | `completed` только после `validate` кандидата (ffprobe + QA); early stop — `completed` с `stop_reason`, не «5 из 5» |
| Переполнение событий | лимит строк с ротацией, ограничение частоты `progress`-событий (≥ 1 с на стадию), переходы состояний — всегда |

## 5. Порядок (R0–R7, соответствие подзадачам плана)

1. R0–R1 — ADR 0012, этот план, схемы, `review/policy.py`, `review/state.py`
   (запуск, раунд, state machine, чекпоинты), `review/versions.py`, API запусков.
2. R2–R3 — `review/bundle.py`, `review/reviewers.py`, `review/issues.py`
   (контракт, дедуп, конфликты, ранг), `review/planner.py` (DAG, impact, бюджет,
   ворота), API замечаний/решений/плана.
3. R4–R5 — `review/repair.py`, `review/compare.py` (hard gates, метрики, судья),
   `review/engine.py` (раунды, stop logic, приём/откат, выбор ревизии), E2E §12.
4. R6a–R6b — `progress.py`: события, дерево, ETA с историей, ресурсы, SSE,
   экспорт логов, инструментирование воркера.
5. R6c — UI: панели «Ревью и улучшение» и «Прогресс», консоль логов, A/B-плеер.
6. R6d–R7 — E2E наблюдаемости, бенчмарк (`REVIEW_BENCHMARK.md`), матрица приёмки.

## 6. Критерии приёмки

- Пять E2E ТЗ §12 на фейках с настоящим ffmpeg: два раунда с ≥ 2 реальными
  правками (кадры финала отличаются от V0 только в затронутом диапазоне, не
  затронутые сцены не перерисованы — по mtime файлов); отказ кандидата и откат к
  V<n>; без мультимодальной модели — деградация и `manual_check`; остановка
  пользователем; исчерпание бюджета. 1 и 5 раундов валидны, максимум не
  превышается, early stop — `completed` с причиной.
- У каждого замечания есть `evidence[]` с `type` и ссылкой на артефакт/кадр/слова;
  замечания без проверяемого evidence не запускают перерисовку без утверждения.
- Версии V0..Vn сравниваются (`comparison.json`, A/B в UI) и восстанавливаются
  (`select-revision`); V0 неизменяем; QA кандидата не хуже baseline (hard gate).
- Прогресс и логи — только из `events.jsonl`; после перезагрузки страницы и
  аварийного рестарта сервиса дерево восстанавливается из файлов; 100 % —
  только после проверки MP4; `awaiting_approval` — отдельное состояние с CTA;
  SSE переживает реконнект с backfill без дублей; экспорт JSONL/CSV; 100 000
  событий читаются по курсору без задержек в UI.
- `./check` зелёный; `docs/ACCEPTANCE.md` дополнена разделом с кодом/тестом/статусом
  на каждый пункт ТЗ.
