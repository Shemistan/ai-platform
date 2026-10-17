---
тип: adr
статус: принято
обновлено: 2026-10-17
---

# 0003. Сервис на stdlib `http.server`, без зависимостей

Записано задним числом (DX-008): решение принято при сборке платформы
(спека `docs/superpowers/specs/2026-10-15-ai-platform-design.md`).

## Контекст

Платформа — локальный сервис одного человека на одном Mac: страницы
вкладок, JSON API, отдача файлов (видео, картинки, аудио с Range). Нагрузка —
единицы запросов в секунду от одного браузера. Тяжёлое (модели, ffmpeg,
narrator) идёт подпроцессами, а не в процессе сервиса. Исходные photogen и
videogen уже были на stdlib-сервере и ванильном JS.

Альтернативы: FastAPI/Flask (+ uvicorn), фреймворк фронтенда со сборкой.

## Решение

- HTTP — `http.server.ThreadingHTTPServer` и свой `Handler`
  (`app/platform/aiplatform/server.py`); маршруты — `match (method, path)`
  в `aiplatform/*/service.py`, общий `Request` — `aiplatform/core/http.py`.
- `dependencies = []` в `app/platform/pyproject.toml`: сервис запускается
  `python3 -m aiplatform.server` на любом Python 3.11+ без venv и `pip`.
- Фронтенд — статические `static/*.html|js|css` без сборки; тесты JS —
  `node --test`, если node есть.

## Последствия

- Установка — `make run`, нечему ломаться при обновлении зависимостей;
  агент в sandbox без PyPI гоняет тесты и сервис.
- Свои руками: разбор пути, JSON-ошибки, проверки Host/Origin/токена,
  Range-отдача, лимиты тела — ~700 строк `server.py` и `core/http.py`, их надо покрывать тестами.
- Нет автогенерации OpenAPI — справочник маршрутов собирает
  `tools/api_routes.py` (`docs/api-routes.md`, DX-005).
- Поток на запрос: долгие операции обязаны уходить в фоновые задачи
  (`core/tasks.py`), иначе держат поток.

## Когда пересматривать

- Сервис станет многопользовательским или выйдет из localhost/LAN в сеть.
- Понадобятся WebSocket/SSE-потоки или асинхронность, которых нет в stdlib.
- Обвязка HTTP начнёт расти быстрее, чем код вкладок.
