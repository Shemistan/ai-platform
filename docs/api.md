# HTTP API платформы

Контракт `app/platform` для встроенного UI и внешних клиентов (CLI, агенты).
Код: `aiplatform/core/http.py` (`HttpError`, `Request`), маршрутизация —
`aiplatform/server.py`, ручки вкладок — `aiplatform/<вкладка>/service.py`.
Тест контракта — `app/platform/tests/test_api_contract.py`. Список всех
маршрутов и команд моста `narrate bridge` — [`api-routes.md`](api-routes.md)
(собирается из кода, `./check` сверяет).

## Общее

- Все ручки — под `/api/`; вкладка — первый сегмент (`/api/studio/…`,
  `/api/photo/…`). Свои: `/api/config`, `/api/status`, `/api/health`,
  `/api/ready`.
- Глаголы: `GET` — чтение без побочных эффектов, `POST` — всё остальное.
  `PUT`/`PATCH`/`DELETE` → `405` с заголовком `Allow: GET, HEAD, POST`.
  Удаление — `POST …/<id>/delete` (браузерная форма и CSRF-проверка одни на
  все действия).
- Тело — JSON (`Content-Type: application/json`), до 1 МБ, если ручка не
  говорит иного; загрузки файлов — сырое тело (`POST …/uploads`).
- `POST` без заголовка `Origin` и без `X-AIP: 1` → `403` (SEC-002).
  С `AIP_AUTH_TOKEN` — `Authorization: Bearer <токен>` или cookie входа.

## Ошибки

Каждый ответ-ошибка (`4xx`/`5xx`) — JSON:

```json
{"error": "Проект не найден.", "code": "not_found"}
```

- `error` — текст для человека (по-русски), его показывает UI.
- `code` — машинный код: клиент различает ошибки по нему, не по тексту.
- Дополнительные поля ошибки лежат рядом, на верхнем уровне (так их уже
  читает UI): `kind` у ошибок движка narrator, `existing` у дубликата
  выпуска, `request_id` у `500`. Новые подробности — объектом `details`.

| Статус | code по умолчанию | Когда |
|---|---|---|
| 400 | `bad_request` | неверный ввод |
| 401 | `unauthorized` | нет токена доступа |
| 403 | `forbidden` | чужой Origin, нет `X-AIP` |
| 404 | `not_found` | нет ресурса или адреса |
| 405 | `method_not_allowed` | глагол не поддерживается |
| 409 | `conflict` | ресурс занят (выпуск выполняется, задача уже идёт) |
| 411 | `length_required` | нет `Content-Length` |
| 413 | `too_large` | тело больше лимита |
| 421 | `bad_host` | чужое имя в `Host` (DNS rebinding) |
| 500 | `internal` | баг; в ответе `request_id`, он же в логе |
| 503 | `unavailable` | зависимость недоступна |

Уточнённые коды (тот же статус, свой `code`):

| code | Статус | Когда |
|---|---|---|
| `engine_unavailable` | 503 | движок narrator не запустился или не ответил (`kind: unavailable`) |
| `engine_rejected` | 400 | движок отверг запрос (`kind: invalid`) |
| `config_broken` | 503 | битый файл настроек сервера (`channels.json`) — чинить сервер, не запрос |
| `schema_too_new` | 409 | файл данных записан более новой версией — правка только в ней (ADR 0009) |
| `stopping` | 503 | сервис останавливается, `POST` не принимаются |
| `not_ready` | 503 | `/api/ready`: проверки не прошли, отчёт — в том же ответе |

Новый код — через `HttpError(status, message, code="…")` и строку в этой
таблице. Без `code=` он берётся из статуса.

## Коды успеха

| Статус | Когда | Заголовки |
|---|---|---|
| 200 | чтение; синхронное действие, результат готов в ответе | — |
| 201 | создан ресурс (задача фото/видео/аудио, выпуск, канал, загрузка) | `Location` — адрес ресурса |
| 202 | асинхронное действие принято, работа идёт в фоне | `Location` — за чем следить, если адрес есть |

**Асинхронное действие = 202 + ссылка на задачу.** В теле — текущее
состояние ресурса или задачи (`{"task": …}`), за продолжением следят опросом
`GET` по ссылке из `Location` или из тела. Сейчас так отвечают:
`studio projects/<id>/run`, `…/publish`, `…/summary`, `stats/refresh`,
`library articles/<id>/<action>`, `library render`, `library unsent/<mode>`,
`news sources/collect`, `news failed/rescue`, `news reviews/<kind>`,
`audio voices/<stem>`, `updates voice-models/<action>`.

## Списки

Длинные списки — с пагинацией по курсору времени:

- `?limit=N` — не больше N записей (новые первыми);
- `?since=<unix-время>` — только изменённые после момента (для опроса).

Списки задач `photo|video|audio/jobs` так и работают (BE-007): без `?limit`
— 100 новых, максимум 1000; идущие задачи `since` не отсекает. Неверные
`limit`/`since` → 400. Остальные ручки пока отдают весь список; перевод — по
мере роста.

## Ресурсы

| Вкладка | Ресурсы |
|---|---|
| `studio` | `config`, `projects` (`POST` — создать; `Idempotency-Key`), `projects/<id>` и действия `run`, `cancel`, `approve`, `publish`, `publish/resolve`, `settings`, `split`, `summary`, `clone`, `archive`, `uploaded`, `stats`, `upload/…`, `artifacts/<name>`, `files/…`; `board`, `preflight`, `stats/refresh` |
| `photo` | `config`, `jobs`, `jobs/<id>` (`cancel`, `upscale`, `image`), `uploads` |
| `video` | `config`, `jobs`, `jobs/<id>` (`cancel`, `video`), `living`, `uploads` |
| `avatar` | `config`, `uploads`, `characters` (`POST` — добавить), `characters/<id>` (`image`, `rename`, `delete`), `jobs`, `jobs/<id>` (`cancel`, `face`, `video`) |
| `audio` | `config`, `preview`, `split`, `library`, `jobs` (`batch`), `jobs/<id>` (`cancel`, `audio`), `voices`, `tasks/<id>` |
| `channels` | `state`, `items` (`POST` — добавить), `items/import`, `items/<id>` (`POST` — правка, `…/delete`), `bot/check`, `subscribers`, `serve` (`start`, `stop`) |
| `library` | `state`, `articles`, `articles/<id>` и действия, `render`, `unsent/<mode>`, `tasks/<id>` |
| `news` | `sources` (`collect`), `failed` (`rescue`), `top`, `reviews`, `tasks/<id>` |
| `settings` | `state`, `toggle`, `section/<name>`, `cleanup` (`run`), `notify-test` |
| `updates` | `voice-models`, `models/check`, `registry` и действия, `tasks/<id>` |

Устаревшие адреса работают как алиасы, новые клиенты их не используют:

| Старый | Новый |
|---|---|
| `/api/channels/channels[/…]` | `/api/channels/items[/…]` |

## Как менять

- Новая ручка ошибается через `HttpError` — `code` придёт сам.
- Смена статуса или адреса — без поломки UI: `ui.js api()` считает успехом
  любой `2xx`; старый адрес оставить алиасом и вписать в таблицу выше.
- Тест `test_api_contract.py` обходит все вкладки реестра и проверяет, что
  у каждой ошибки есть `error` и `code`.
