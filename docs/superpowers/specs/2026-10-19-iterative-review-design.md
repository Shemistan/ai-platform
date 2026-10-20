---
тип: spec
статус: реализовано
обновлено: 2026-10-19
план: 2026-10-19-iterative-review.md
---

# Iterative Review & Refinement и панель наблюдаемости «Монтажа» (PRD-028)

## Зачем

После сборки ролика (MASTER_SPEC, PRD-027) автор получает V0. ТЗ
`ITERATIVE_REVIEW_SPEC.md` добавляет цикл: независимые роли смотрят версию,
замечания сводятся в план точечных правок, правки исполняются, кандидат проходит
технический QA и сравнение с baseline, принимается или откатывается; 1–5 раундов с
ранней остановкой. Раздел 14 ТЗ — панель прогресса и логов всего конвейера из
настоящих событий backend. Проекция на стек — ADR 0012, gap analysis и порядок —
`docs/ITERATIVE_REVIEW_IMPLEMENTATION_PLAN.md`.

## Данные

Каталог проекта `montage/projects/<pid>/`:

```
review/
  runs/<run_id>/run.json                 ReviewRun: policy, state, round, rounds[], budget, stop_reason, lock
  runs/<run_id>/rounds/<n>/round.json    стадии раунда с временем, счётчики, решение
  runs/<run_id>/rounds/<n>/bundle.json   манифест бандла: прокси, кадры, аудио-замеры, тексты
  runs/<run_id>/rounds/<n>/bundle/       proxy.mp4, frames/<frame>.jpg, audio.json
  runs/<run_id>/rounds/<n>/reports/<role>.json   ReviewerReport (issues[], degraded, manual_check, llm/measure)
  runs/<run_id>/rounds/<n>/issues.json   консолидированные ReviewIssue со статусами и decisions[]
  runs/<run_id>/rounds/<n>/repair_plan.json  RepairPlan: actions[] (DAG), impact, estimate, policy_check
  runs/<run_id>/rounds/<n>/comparison.json   hard gates, objective, subjective, decision
  versions/V<n>/{version.json, output.mp4, timeline.json, storyboard.json, qa_report.json, plan.json,
                 edl.json, timemap.json, subtitles.vtt}
events.jsonl      run_event, seq растёт; progress.json — снимок дерева; resources.jsonl — замеры
```

`V0` — baseline при создании запуска (жёсткая ссылка на `output.mp4`, манифесты
копией). `versions.save` отказывает, если `V<n>` уже есть. `run.lock.project_updated_at`
— optimistic locking: проект изменён снаружи → `stale_revision`.

## Запуск и раунд

Состояния запуска: `idle → preparing → reviewing → consolidating → awaiting_approval →
repairing → rendering → validating → comparing → accepted | rejected → (next_round |
completed)`, плюс `awaiting_decision` (спорное сравнение в ручном режиме), `paused`,
`cancelled`, `failed`, `blocked`. Чекпоинт `run.json` после каждой стадии; стадия
идемпотентна (артефакт есть → пропуск). Воркер «Монтажа» исполняет действие
`review` (arg = run_id): крутит стадии, пока не упрётся в ожидание человека, паузу,
конец раунда или конец запуска. `pause` взводит cancel: текущая стадия
прерывается, state = `paused`, resume ставит действие снова. `stop` → `cancelled`
(после раунда — `completed` с `stop_reason: user`).

Политика (`review_policy`): `enabled`, `max_rounds` 1..5 (2), `mode`
`manual_approval|auto_safe|autonomous`, `review_profile`
`balanced|educational|dynamic_youtube|minimalist`, `quality_target` 0..1,
`max_gpu_minutes`, `max_wall_time_minutes`, `max_new_assets`,
`max_render_attempts_per_issue` (2), `allow_narrative_edits` (false),
`protected_ranges` [{from,to}] (слова исходника), `locked_scenes` [], `protected_claims`
[], `reviewer_roles` [], `stop_when_no_material_gain` (true).

## Роли

Один immutable бандл на раунд: прокси 640p (`proxy.mp4`), кадры начала/середины/конца
каждой сцены + равномерно раз в 10 с (`frames/f<frame>.jpg`), аудио-замеры
(`ebur128` I/LRA/TP, `silencedetect`, `astats` пик, уровни на границах сегментов),
`signalstats` YAVG/YDIF по кадрам 2 к/с, тексты: транскрипт результата с кадрами,
биты, знания, сцены, стиль, таймлайн, QA, история, замечания прошлых раундов.

| Роль | Вид | Evidence |
|---|---|---|
| `technical_qa` | measure: `qa.mechanical` версии + отсутствующие файлы | observed_video/observed_audio |
| `sound` | measure: громкость к −14 LUFS, TP, пик, тишина, короткие сегменты, скачок уровня на склейке | observed_audio |
| `accessibility` | measure: скорость чтения субтитров (> 20 зн/с), переполнение, вспышки (YDIF > 40 ≥ 3 раз за 1 с), тёмные кадры под субтитрами | observed_video/transcript |
| `motion_director` | heuristic: сцена кончается раньше последнего слова бита (> 1 с) → `motion_timing`; длиннее бита на 2 с → shorten; перекрытия; плотность | transcript/heuristic, `degraded` без VLM |
| `art_director` | heuristic: ревизия стиля у сцен, однообразие шаблонов (> 50 % один), props без текста | heuristic, `degraded` |
| `composition` | heuristic: сцена в первые 2 с, последние 2 с, `lower_third` вместе с субтитрами в кадре, сцены подряд ближе 3 с | heuristic, `degraded` |
| `executive`, `narrative`, `retention`, `educational` | llm: `### task: montage_review_<role>` с текстами бандла; ответ — JSON `{issues: [...]}`, валидируется и чистится | transcript/source_document/heuristic |

Контракт замечания — схема `review_issue` (поля из ТЗ §5 + `change`:
структурная правка `{op, scene_id?, frames?, props?, max_words?, from?, to?}`,
ops: `scene_extend | scene_shorten | scene_shift | scene_disable | scene_props |
caption_words | cut_words | none`). Валидатор: диапазоны в пределах версии, сцены
существуют, evidence непуст; иначе замечание помечается `invalid` и не
участвует.

## Консолидация и план

1. Дедуп: одна категория и пересечение диапазона ≥ 50 % или общий `scene_id` →
   объединение (роли и evidence суммируются, уверенность — максимум).
2. Конфликты: противоположные ops по одной сцене (`extend` ↔ `shorten`, `shift` ±,
   `disable` ↔ любая правка). Арбитр: выше ранг → выше уверенность → дешевле;
   равные — `deferred`, эскалация человеку. Выбор — в `decisions.jsonl`.
3. Ранг: `technical` (blocker/major техники) > `factual` > `clarity` > `aesthetic`;
   внутри — severity, уверенность, influence, стоимость, риск, защищённые области
   (замечание по защищённой области или locked сцене → `rejected: protected`).
4. План: действие на замечание (`op` → action) с `depends_on` (действия одной
   сцены — последовательно; `cut_words` — раньше сценных; `render` и `qa` —
   после всех), `affected_frames`, `asset_hash_before/after`, `estimate`
   (`render_s` ≈ 3 × длительность сцены + 0,3 × длительность ролика на финал),
   `tests` (acceptance_check), `rollback` = версия `V<n>`.
5. Политика: `allow_narrative_edits=false` → `cut_words` отклонены; `max_new_assets`
   — перерисовки сверх лимита `deferred`; `evidence_type: heuristic` без
   `observed_*` → только через утверждение; `mode`: `manual_approval` — всё ждёт;
   `auto_safe` — безопасные ops (extend/shorten/shift ≤ 2 с, caption_words)
   принимаются, остальное ждёт; `autonomous` — всё в пределах политики, смысловые
   удаления и `scene_disable`/`scene_props` на защищённых claim — ворота.

## Исполнение и сравнение

`repair.apply`: правки манифестов (storyboard через `storyboard.edit` — новый хэш
только у правленой сцены; `caption_words` → `options.caption_words`; `cut_words` →
план вырезок + `_write_edl` → TimeMap, якоря, субтитры пересчитываются), затем
`_render` проекта (сегменты и сцены из кэша, перерисовка только изменённых),
QA кандидата. Попытки действия ≤ `max_render_attempts_per_issue`, потом
`failed`. Кандидат → `V<n+1>`.

`compare`: hard gates — файл читается, длительность = таймлайну ±1 кадр (+ хвост
loudnorm), AV sync (длины дорожек ±1 кадр), новых mechanical `error` нет,
защищённые слова в тех же сегментах, locked сцены с теми же хэшами, sha256
источников те же. Objective — счётчики QA, переполнения субтитров, пик/громкость,
дрейф, пропавшие ассеты. Subjective — судья `montage_review_judge` (без самооценки
исполнителя): пары A/B по измерениям `clarity, style, motion, pacing, distraction,
accuracy` с evidence; без LLM — `human_review`. Веса профиля (`balanced` и т. д.)
применяются к субъективной части и показываются отдельно от технических.
Решение: hard fail → `rejected`; objective хуже → `rejected`; субъективный
взвешенный дельта > 0,05 или закрыто ≥ 1 замечание без ухудшения → `accepted`;
спорно → ручной режим `awaiting_decision`, autonomous — `rejected` (откат).
Отклонён → манифесты и `output.mp4` возвращаются из `V<n>`; следующий раунд — от
последней принятой версии.

Ранняя остановка: нет исполнимых действий; `stop_when_no_material_gain` и
дельта < порога; бюджет исчерпан; замечания повторяются (≥ 80 % отпечатков как в
прошлом раунде); осцилляция (обратное действие по той же сцене); пользователь.
`max_rounds` — верхняя граница.

## Наблюдаемость

`progress.Tracker` в `MontageProjects`: события `run_started`, `stage_started`,
`stage_progress` (не чаще раза в секунду на стадию), `stage_finished`,
`job_started/progress/finished` (ffmpeg/asr/llm/hyperframes; `fps`, `speed` из
`-progress`), `task_*` (сцена, сегмент, роль-ревьюер), `log`, `round_*`,
`reviewer_finished`, `issues_consolidated`, `approval_required`, `repair_started`,
`scene_rendered`, `qa_failed`, `comparison_ready`, `revision_accepted/rejected`,
`run_completed/blocked`. Поля — схема `run_event`.

Дерево `progress.tree`: Project → Run (действие) → Phase → Round → Stage → Job/Task
→ Attempt; на узле `status`, времена, `counts {total, completed, running, failed,
skipped, remaining}`, `percent`, `eta {seconds, low, high, confidence}`,
`blocking_reason`, `current_action`, `artifacts`, `parent_id`, `correlation_id`.
Проценты — по единицам стадии (секунды звука, сегменты, сцены, роли); веса — из
`montage/stats.json` (EMA секунд на единицу по этой машине; мало замеров —
`confidence: low`, fallback — веса `stages.py`). Неизвестный total — `indeterminate`
с текущей операцией. `completed` только после `validate`. Пересчёт total
(динамические задачи) — событие `plan_changed` с причиной.

API (`/api/montage/projects/<pid>/…`): `review-runs` (POST/GET),
`review-runs/<rid>` (GET), `…/pause|resume|stop`, `…/rounds/<n>/issues`,
`…/rounds/<n>/decisions`, `…/rounds/<n>/repair-plan`, `…/rounds/<n>/execute`,
`…/rounds/<n>/comparison`, `…/rounds/<n>/decide`, `…/select-revision`,
`…/versions`, `…/versions/<v>/video`, `…/rounds/<n>/bundle/frames/<name>`,
`…/rounds/<n>/progress`; `progress`, `agents`, `runs`, `events?cursor&limit&…`,
`events/stream?last_event_id`, `logs/export?format=jsonl|csv`, `resources`.

UI (вкладка «Монтаж»): панель «Ревью и улучшение» (настройки политики, Start/Pause/
Resume/Stop, лента раундов с полосами стадий, карточки ролей, список замечаний с
фильтрами severity/scene/role и кликабельным таймкодом, кадры-улики, план с
оценкой и кнопками принять/отклонить/защитить, A/B-плеер с синхронным
воспроизведением, версии с причинами, выбор ревизии); панель «Прогресс» (обзор,
дерево стадий, агенты, ресурсы, история запусков, консоль логов с фильтрами,
виртуализацией, паузой автопрокрутки, экспортом). Цвет — не единственный
индикатор; клавиатура и `aria`.
