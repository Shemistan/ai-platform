---
тип: spec
статус: утверждено
обновлено: 2026-10-17
план: 2026-10-17-job-queue.md
---

# Одна очередь задач `core.jobs` (BE-007)

## Зачем

`core/tasks.py`, `photo/jobs.py`, `video/jobs.py`, `audio/jobs.py` — четыре
копии одной очереди: `_save/_load/_finish/_work/cancel/wait/forget/list`.
Копии расходятся. `Tasks` чистит историю (`KEEP=60`), а JobManager-ы — нет:
живой `/api/photo/jobs` отдавал 149 задач (160 КБ) на каждый опрос. В `_work`
фото/видео/аудио стояли `self._jobs[job_id]` и `self._specs.pop(job_id)`:
если задачу отменили в очереди, а уборка её забыла, воркер падал с
`KeyError`, и вкладка навсегда оставалась «в очереди».

## Что уже сделано (первый шаг)

- `_work` трёх JobManager-ов пропускает исчезнувшую задачу (`.get()`/`pop(…, None)`).
- `core/jobs.py`: `select(jobs, limit, since)` и `list_args(req)`;
  `GET /api/{photo,video,audio}/jobs?limit=&since=`, без `limit` — 100.
- Формат `job.json` и карантин битых — общие (`core/schema.py`, BE-008).

## Решение (целевое)

`core/jobs.JobQueue[Spec]` — одна реализация очереди:

- хранение: `<dir>/<id>/job.json` (как фото/видео/аудио; задачи `core.tasks`
  переходят с `<dir>/<id>.json` миграцией при загрузке);
- жизненный цикл: `submit → queued → running → done|failed|canceled`,
  `cancel`, `wait(cancel_event)`, `forget`, восстановление после рестарта
  (`running/queued` → `failed`, «сервис был перезапущен»);
- воркер: один поток, GPU-замок по `gpu` спеки, лимиты времени из
  `runner.LIMITS` (REL-004);
- история: `keep` последних завершённых в памяти (на диске — пока уборка
  `core/cleanup.py` не удалит по возрасту), список — через `select`;
- хуки вкладки: `command(spec, job) -> argv`, `progress(line) -> dict | None`,
  `result_path(job) -> Path`, `view(job) -> dict` (поля карточки).

Публичный API модулей (`create/get/list/cancel/wait/forget/*_path`) не
меняется: вкладки и сценарии студии зовут его как раньше.

## Вне рамок

- Клиентская пагинация списка — FE-002.
- Несколько воркеров на очередь.
