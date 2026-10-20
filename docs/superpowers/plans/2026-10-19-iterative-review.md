---
тип: plan
статус: завершено
обновлено: 2026-10-19
спека: 2026-10-19-iterative-review-design.md
---

# План: Iterative Review и наблюдаемость «Монтажа» (PRD-028)

**Цель:** 1–5 раундов ревью готового ролика ролями-агентами с консолидацией,
планом, точечной пересборкой, regression gate и откатом; панель прогресса и
логов всего конвейера из событий backend. **Спека:**
`../specs/2026-10-19-iterative-review-design.md`, порядок и критерии —
`docs/ITERATIVE_REVIEW_IMPLEMENTATION_PLAN.md`, проекция — ADR 0012.

**Ограничения:** stdlib Python, ванильный JS, файлы, `claude -p` (фейк по
`### task:`), ffmpeg настоящий в тестах, `./check` зелёный на каждой подзадаче,
`docs/context.md` правится в том же коммите.

Подзадачи — ветки от `staging`: `./check` → коммит → `./finish` (push — человек).
Факт: подзадача 1 — коммит `ca1e78b` (`worktree-iterative-review`); подзадачи 2–6 оказались связаны
общим движком раундов и вошли одним коммитом в ветке `iterative-review/review`.

## 1. R0–R1: основание (ветка `worktree-iterative-review`)

1. ADR 0012, `docs/ITERATIVE_REVIEW_IMPLEMENTATION_PLAN.md`, спека, этот план.
2. Схемы `contracts/montage/`: `review_policy`, `review_run`, `review_round`,
   `reviewer_report`, `review_issue`, `repair_plan`, `repair_action`,
   `review_comparison`, `review_version`, `run_event`.
3. `montage/review/policy.py`: `DEFAULT`, `clean(payload, base) -> dict`
   (ошибки → `PolicyError`), `PROFILES` (веса), `is_safe(action)`, `touches_protected`.
4. `montage/review/state.py`: `new_run(pid, policy, base_version, lock) -> dict`,
   `STATES`, `TRANSITIONS`, `advance(run, state, **fields)`, `checkpoint(folder, run)`,
   `load_run/list_runs`, `new_round(run) -> dict`, `round_path`, `interrupted()`.
5. `montage/review/versions.py`: `save(folder, n, label, *, round, parent, accepted)`
   (жёсткая ссылка на `output.mp4`, копии манифестов, отказ при повторе),
   `listing`, `restore(folder, n)` (манифесты + `output.mp4` обратно), `diff(a, b)`.
6. API: `POST/GET review-runs`, `GET review-runs/<rid>`, `pause|resume|stop`,
   `versions`, `versions/<v>/video`. Тесты `tests/test_montage_review.py`
   (политика, переходы, версии, API без движка).

## 2. R2–R3: бандл, роли, консолидация, план (ветка `iterative-review/review`)

1. `montage/review/bundle.py`: `build(config, folder, version_dir, out_dir, cancel) ->
   dict` (прокси, кадры по сценам и раз в 10 с, `signalstats`, `ebur128`, `astats`),
   `texts(folder, version_dir, previous_issues) -> dict`.
2. `montage/review/reviewers.py`: `ROLES` (id, label, kind, rank), `run_role(role,
   bundle, ctx) -> report`; measure/heuristic-роли и промпты llm-ролей; фейк
   `montage_review_<role>` в `fake_llm.py` (`FAKE_REVIEW_*`).
3. `montage/review/issues.py`: `validate(issue, version) -> list[str]`, `fingerprint`,
   `dedup`, `conflicts`, `arbitrate`, `rank`, `apply_policy`.
4. `montage/review/planner.py`: `plan(issues, version, policy, config) -> RepairPlan`
   (DAG, impact, estimate, approval).
5. `montage/review/engine.py` (стадии preparing → consolidating →
   awaiting_approval) + API `rounds/<n>/issues`, `decisions`, `repair-plan`,
   `bundle/frames/<name>`. Тесты: дедуп/конфликты/ранг/политика/план, роли на
   фикстуре.

## 3. R4–R5: исполнение, сравнение, раунды (ветка `iterative-review/repair`)

1. `montage/review/repair.py`: `apply(projects, project, plan, cancel) -> results`
   (правки манифестов, `_render`, попытки, `failed`).
2. `montage/review/compare.py`: `hard_gates`, `objective`, `subjective` (судья
   `montage_review_judge`, фейк), `decide(policy, …)`.
3. `engine.py`: repairing → rendering → validating → comparing → accepted/rejected,
   откат, `next_round`, early stop, бюджет, осцилляция, `select-revision`,
   `execute`, `decide`. Тесты E2E §12 (`tests/test_montage_review_e2e.py`).

## 4. R6a–R6b: события и прогресс (ветка `iterative-review/progress`)

1. `montage/progress.py`: `Events` (append с `seq`, курсор, фильтры, ротация,
   экспорт jsonl/csv), `Tracker` (стадии, jobs, tasks, rate limit),
   `tree(project, events, stats)`, `eta`, `Stats` (`stats.json`, EMA), `resources()`.
2. `core/http.py`: `Request.send_sse(iterator, last_event_id)`; `projects.py` —
   инструментирование `_enqueue/_stage/_finish/_ffmpeg_run/_note`, `scenes`, ревью.
3. API `progress`, `events`, `events/stream`, `logs/export`, `resources`, `agents`,
   `runs`, `review-runs/<rid>/rounds/<n>/progress`. Тесты: агрегация, проценты,
   ETA, 100 000 событий по курсору, SSE реконнект/backfill/дедуп.

## 5. R6c: UI (ветка `iterative-review/ui`)

1. `static/montage/review.js` (чистые функции: фильтры замечаний, строки раундов,
   A/B-синхронизация, подписи версий), `static/montage/progress.js` (дерево, окно
   виртуализации логов, фильтры, ETA-подписи), панели в `app.js`, стили,
   `EventSource` с fallback на опрос. Тесты `tests/js/montage-review.test.mjs`,
   `montage-progress.test.mjs`.

## 6. R6d–R7: приёмка (ветка `iterative-review/acceptance`)

1. E2E наблюдаемости (рестарт, SSE, экспорт, 100 000 событий), бенчмарк
   `docs/REVIEW_BENCHMARK.md`, `docs/ACCEPTANCE.md` (раздел ITERATIVE_REVIEW_SPEC),
   `docs/user-guide.md`, `docs/api.md`, `docs/api-routes.md`, карта.
