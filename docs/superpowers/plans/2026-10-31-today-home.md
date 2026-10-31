---
тип: plan
статус: завершено
обновлено: 2026-10-31
спека: 2026-10-31-today-home-design.md
---

# План: «Сегодня» (UX-031)

Одна ветка `worktree-ui-simplify`, после UX-030…UX-038 и DS-019.

1. `static/core/today.js` — чистые функции: `guessIntent(text | file)` → сценарий,
   подпись и кнопка; `pickAttention(projects, n)` → до n выпусков «Ждут тебя»;
   `greeting(hour)`. JS-тест `tests/js/today.test.mjs`.
2. `static/today/` — `index.html`, `app.js`, `style.css`: поле, предложение,
   «Ждут тебя», «Идёт сейчас»; создание через существующие API (спека).
3. Сервер: `HOME = "/today/"`; `studio.nav()` — вкладка «Сегодня» (`home`) первой
   в «Выпусках». Тесты шапки и редиректа `/`.
4. Документация: `docs/user-guide.md` (вкладки), `docs/context.md` (вкладка
   `today/`), задача UX-031.
5. Проверка в браузере на демо-данных: тёмная тема, 1200×610, ширина ≤960 px.
