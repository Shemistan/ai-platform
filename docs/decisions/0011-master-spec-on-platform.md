---
тип: adr
статус: принято
обновлено: 2026-10-19
---

# 0011. MASTER_SPEC (студия видеопродакшена) — внутри платформы, а не новым монорепо

## Контекст

`MASTER_SPEC.md` (вне git, корень checkout) описывает local-first студию
автоматического видеопродакшена: talking-head запись → расшифровка → черновой
монтаж (EDL) → привязка к материалам → нарративные биты → стиль-библия →
раскадровка → ≥12 детерминированных моушн-шаблонов → таймлайн-манифест → QA
с ремонтом → экспорт → ревизии проекта. Архитектура в ТЗ: Next.js + FastAPI +
Pydantic + SQLAlchemy/SQLite + Remotion + durable queue + WebSocket.

Человек просит сделать это **в рамках готового сервиса**. У платформы уже
есть решения, которые ТЗ противоречат буквально, но не по смыслу: stdlib HTTP
и ванильный JS (ADR 0003), файлы вместо БД (ADR 0007), LLM через `claude -p`
(ADR 0006), моушн на HyperFrames (HTML + GSAP, PRD-022), один GPU-замок
(ADR 0004). Вкладка «Монтаж» уже закрывает ingest → ASR → EDL → сборка →
самопроверка → вставки; «Моушн» — команду ролей-агентов (дизайнер, режиссёр,
техред, аниматор).

## Решение

ТЗ реализуется как расширение `montage/` (PRD-027). Проекция:

| ТЗ | Платформа | Почему |
|---|---|---|
| Next.js + TypeScript, generated TS types | ванильный JS без сборки, типы — JSON Schema в `contracts/montage/` | ADR 0003; сборки в проекте нет |
| FastAPI + Pydantic v2 | stdlib `http.server`; схемы (§7) — JSON Schema + проверка `montage/schemas.py` (подмножество: type/required/properties/items/enum/min/max) | ADR 0003 |
| SQLAlchemy + SQLite/PostgreSQL, Alembic | файлы проекта `montage/projects/<id>/*.json`, версия схемы и миграции при чтении (ADR 0009) | ADR 0007 |
| Remotion | HyperFrames: HTML + GSAP, рендер в ProRes 4444 с альфой; каталог шаблонов — `montage/templates.py` | PRD-022, уже стоит в проекте |
| Durable queue (Redis/Celery) | воркер модуля + состояние в `project.json`; прерванное действие после рестарта → `interrupted`, повтор берёт кэш сегментов и сцен по хэшам | один пользователь, один процесс (ADR 0007) |
| WebSocket/SSE | опрос `GET` (`poller` в UI) | как у всех вкладок |
| Реестр моделей и адаптеры с `probe()` | `montage/capabilities.py`: адаптеры = CLI (воркер ASR, `claude -p`, HyperFrames, ffmpeg, Blender, pdftotext), `probe` реальной командой, `/api/montage/capabilities`; маршрутизация детерминированная: нет движка → сцена `blocked` с причиной | ADR 0005/0006 — всё внешнее идёт подпроцессами |
| Blender 3D | только capability-gate: есть бинарник → `available`, нет → `unavailable`; шаблонов 3D нет | не ставить то, чего нет на машине |
| Auth/LAN/rate limits | уже есть: Host/Origin/`X-AIP`, `AIP_AUTH_TOKEN`, CSP, лимиты тел | `server.py`, SEC-* |
| Role agents | промпты ролей в `montage/plan.py`, `montage/knowledge.py`, `montage/narrative.py`, `montage/storyboard.py`; каждый вызов — запись в `decisions.jsonl` (роль, модель, версия промпта, хэши входа/выхода, уверенность) | ADR 0006 |
| Файлы документации ТЗ (`ARCHITECTURE`, `AGENTS`, `API`…) | `docs/context.md` (архитектура), `docs/api.md` + `api-routes.md`, `docs/configuration.md` (безопасность и переменные), спека `docs/superpowers/specs/2026-10-19-montage-master-spec-design.md`; новое — `docs/ENVIRONMENT_AUDIT.md`, `docs/TIMEBASE.md`, `docs/ACCEPTANCE.md` | не плодить дубли с разными именами |

Что ТЗ требует, а здесь **сознательно не делается** (в матрице приёмки — «не
реализовано» или «частично»): PostgreSQL и многопользовательский режим; ссылки
как источники материалов (сеть выключена); drag-and-drop таймлайн с волной —
вместо него таймлайн-манифест и правка сцен по кадрам; генеративный B-roll
внутри раскадровки (есть вкладки «Фото»/«Видео»); музыка и дакинг (музыки
нет); диаризация.

## Последствия

- Ничего нового в зависимостях: ТЗ ложится на тот же стек, тесты идут на
  фейках, как у всего проекта.
- Схемы данных в `contracts/montage/` — единый контракт бэкенда, фейков и UI;
  проверяют тесты.
- Честные статусы: `unavailable`/`blocked` с причиной вместо поддельных
  артефактов — правило для каждого шага.
- Полнота по ТЗ фиксируется матрицей `docs/ACCEPTANCE.md`: требование → код →
  тест → статус.

## Когда пересматривать

- Появится второй пользователь или второй процесс-писатель (тогда БД и очередь).
- HyperFrames перестанет справляться с типами сцен (тогда Remotion или свой рендер).
