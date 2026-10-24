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
| `photo` | `config`, `jobs`, `jobs/<id>` (`cancel`, `delete` — 409 на идущую или с идущим апскейлом, `upscale`, `image`), `uploads` |
| `video` | `config`, `jobs`, `jobs/<id>` (`cancel`, `delete` — 409 на идущую, `video`), `living`, `uploads` |
| `avatar` | `config`, `uploads`, `characters` (`POST` — добавить), `characters/<id>` (`image`, `rename`, `delete`), `jobs`, `jobs/<id>` (`cancel`, `face`, `video`) |
| `montage` | `config`, `capabilities` (`GET` — реестр, `POST …/probe` — с запуском команд), `rules`, `projects` (`POST` — создать; `options.mode`: `reviewed\|autonomous\|manual`), `projects/<id>` (`POST` — название, настройки, сдвиг экрана) и действия `sources/<head\|screen>` (тело — видео), `sources/<role>/delete`, `source/<role>`, `start`, `plan` (`GET`, `POST` — правка), `approve` (`{gate: edl\|storyboard\|export}`; в `reviewed` сборка без `edl` → 409), `replan`, `revise`, `documents` (`GET`; `POST` — файл телом, имя в `X-File-Name`; `…/<id>/delete`), `storyboard/run` (знания → биты → стиль → раскадровка), `storyboard` (`GET` — знания, биты, стиль, сцены; `POST` — правки сцен `{scenes: [{id, enabled?, edit_start_frame?, duration_frames?, template_id?, props?}]}`), `scenes/<sid>/redo` (`{text}`), `scenes/<sid>/<thumb.png\|index.html\|scene.json>`, `style` (`GET`, `POST` — новая ревизия), `render`, `timeline`, `qa`, `export` (`{preset, override_reason?}`; при `gates.mechanical == fail` без причины → 409), `exports` (список; `exports/<n>/<file\|provenance.json\|project.zip>`), `revisions` (список; `revisions/<n>/restore`), `cancel`, `delete`, `decisions`, `timemap`, `video`, `subtitles.srt`, `subtitles.vtt`, `edl.json`, `source_manifest.json`, `storyboard.json`, `timeline.json`, `qa_report.json`; ревью (PRD-028): `review-runs` (`GET` — запуски; `POST` — создать, тело = политика `{max_rounds 1..5, mode manual_approval\|auto_safe\|autonomous, review_profile, …}`, 409 без готового ролика или при открытом запуске), `review-runs/<rid>` (`GET`), `review-runs/<rid>/<start\|pause\|resume\|stop>` (409 не из того состояния; проект изменён вне запуска → 409 `stale_revision`), `review-runs/<rid>/rounds/<n>/issues` (`GET` — замечания, отчёты ролей, счётчики), `…/rounds/<n>/decisions` (`POST` `{issues: [{issue_id, status: accepted\|rejected\|deferred, comment?}], locked_scenes?, approve_plan?}` — только в `awaiting_approval`), `…/rounds/<n>/repair-plan` (`GET`), `…/rounds/<n>/execute` (`POST` — утвердить план целиком), `…/rounds/<n>/comparison` (`GET`; `pending: true`, пока нет), `…/rounds/<n>/decide` (`POST` `{decision: accept\|reject, reason?}` — только в `awaiting_decision`), `…/rounds/<n>/bundle/<frames/fNNNNNN.jpg\|proxy.mp4\|bundle.json>` (кадры-улики), `review-runs/<rid>/select-revision` (`POST` `{version: "V2"}` — вернуть версию в проект), `versions` (V0..Vn), наблюдаемость (§14): `progress` (`GET` — дерево Project → Run → Phase → Round → Stage → Job → Task → Attempt, ETA с уверенностью), `events` (`GET ?cursor&limit&type&stage_id&agent_id&job_id&task_id&severity&min_severity&status&round_id&run_id&q&since&until` — страница по курсору `seq`), `events/stream` (`GET`, SSE: `snapshot` + backfill после `Last-Event-ID`/`last_event_id` + живые события + `: ping`; закрывается через 10 мин — клиент переподключается с курсором), `logs/export` (`GET ?format=jsonl|csv` + те же фильтры), `resources` (`GET` — только измеримое; GPU `unavailable`), `agents` (`GET`), `runs` (`GET` — история запусков), `review-runs/<rid>/rounds/<n>/progress` (`GET`), `versions/<V>/<video\|version.json\|timeline.json\|storyboard.json\|qa_report.json>` |
| `motion` | `config`, `montage` (готовые ролики «Монтажа»), `projects` (`POST` — создать; `montage: <id>` — из «Монтажа»), `projects/<id>` (`POST` — название, настройки) и действия `source` (`POST` — видео телом, `GET` — смотреть), `start`, `scenes` (`POST` — своя сцена `{at, text}`), `scenes/<sid>` (`POST` — `{enabled}`), `scenes/<sid>/redo` (`{text}`), `scenes/<sid>/<preview.mp4\|poster.jpg\|scene.mov\|index.html>`, `package`, `cancel`, `delete` |
| `transcribe` | `config` (модели, языки, форматы), `jobs` (`GET` — список; `POST` — создать `{title?, options: {model?, language: ru\|en, terms?}}`), `jobs/<id>` (`GET` — с полным текстом и абзацами; `POST` — название, настройки) и действия `source` (`POST` — видео или аудио телом, имя в `X-File-Name`; `GET` — слушать), `start` (тело — те же `options`, перезапуск после готового — новой моделью), `files/<text.txt\|text.md\|subtitles.srt\|subtitles.vtt\|words.json>` (`?inline=1` — без вложения), `cancel`, `delete` |
| `audio` | `config`, `preview`, `split`, `library`, `jobs` (`batch`), `jobs/<id>` (`cancel`, `delete` — 409 на идущую, `audio`), `voices`, `tasks/<id>` |
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
