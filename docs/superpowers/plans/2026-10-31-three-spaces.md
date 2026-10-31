---
тип: plan
статус: в работе
обновлено: 2026-10-31
спека: 2026-10-31-three-spaces-design.md
---

# План: три пространства (PRD-033)

Ветка `worktree-ui-simplify` (продолжение UX-030…UX-038). Подзадачи по порядку,
каждая — свой коммит с тестами и проверкой в браузере.

1. **Альбом** — `static/core/album.js` (+ чистые функции `albumState`/`parseHash`,
   JS-тест) и стили в `ui.css`.
2. **Генерация** — `static/generate/`: входы и альбом по задачам фото, видео, аудио.
3. **Сценарии новостей** — `studio`: `weekly`, `monthly` (шаг «текст» через narrator,
   `studio/news.py`), `max_minutes` у `digest`/`weekly`/`monthly` и ужатие текста;
   тесты на фейках (`fake_narrate`).
4. **Новости** — `static/newsroom/`: три входа и альбом выпусков.
5. **Статьи** — `static/articles/`: вход и альбом выпусков `article`/`post`.
6. **Шапка и главная** — `NAV_GROUPS`: три пространства + шестерёнка; «Сегодня» —
   три карточки-входа; тесты шапки.
7. **Документация** — `docs/user-guide.md`, `docs/context.md`, `docs/api.md`.
