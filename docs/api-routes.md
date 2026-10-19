# Справочник HTTP API

Все маршруты сервиса `app/platform` и команды моста `narrate bridge` — одной
таблицей на модуль. Таблицы собирает `tools/api_routes.py` из кода
(`case ("GET"|"POST", […])` в `aiplatform/server.py` и
`aiplatform/*/service.py`); `./check` падает, если таблица отстала от кода:

```sh
python3 tools/api_routes.py   # пересобрать после нового или изменённого маршрута
```

Описание строки — комментарий прямо над `case` или первая строка docstring
метода, который ветка зовёт. Пустое «Что делает» лечится комментарием над
`case`, а не правкой этого файла.

Правила контракта — глаголы, формат ошибок, коды, `X-AIP`, токен, лимиты —
в [`api.md`](api.md); здесь только список маршрутов и то, что не выводится
из кода. Пример:

```sh
curl -s http://127.0.0.1:8780/api/status
curl -s -X POST -H 'X-AIP: 1' -H 'Content-Type: application/json' \
  -d '{"step": "text"}' http://127.0.0.1:8780/api/studio/projects/<id>/run
```

## Общие маршруты: что в ответе

### `GET /api/config`

Шапка страниц и баннер «Проблемы»:

```json
{
  "backend": "real",
  "version": "2026.10.17",
  "tabs": [{"id": "studio", "label": "Сценарии"}, …],
  "problems": ["ffmpeg не найден: …", "Каталог моделей … не найден — …"]
}
```

- `backend` — `real` или `fake` (`make demo`, `AIP_BACKEND=fake`).
- `tabs` — модули в порядке шапки: `id` = префикс `/api/<id>/…`.
- `problems` — чего не хватает работающему сервису, строки для человека
  без дублей: ffmpeg, шрифт с кириллицей, каталог моделей, плюс
  `problems()` каждого модуля (narrator устарел или не найден, нет claude
  CLI, нет каналов и т.п.). Пустой список — всё в порядке. Те же строки —
  `⚠` в консоли `make run` и проверка `problems` в `/api/ready`.

### `GET /api/status`

Опрашивается шапкой каждые несколько секунд:

```json
{"gpu": "Фото", "waiting": 1, "gpu_since": 1760000000.0, "gpu_held_s": 42, "studio_attention": 2}
```

- `gpu` — кто держит общий GPU-замок (`null` — свободен), `waiting` —
  сколько задач ждёт замка.
- `gpu_since` / `gpu_held_s` — только когда замок занят: с какого момента
  и сколько секунд (видно зависшую модель).
- Остальные ключи — счётчики модулей (`badges()`), сейчас
  `studio_attention` — выпуски, ждущие человека.

### `GET /api/health` и `GET /api/ready`

`health` — живость: `{"ok": true, "version", "uptime_s", "schedule"}`
(`schedule` — расписание дайджеста и итог последнего запуска). `ready` —
готовность выпустить дайджест: `{"ok", "failed": [имена], "checks":
[{"name", "ok", "detail"}], "problems"}`, при `ok: false` — код `503`.
Как ими пользоваться в эксплуатации — runbook (REL-015).

## Мост `narrate bridge`

Платформа зовёт `narrate bridge <команда> [аргументы]` подпроцессом
(`aiplatform/core/narrator.py`, `Narrator.bridge`). Контракт:

- Вход: аргументы командной строки; тексты (`preview`, `split`,
  `article-new`, `article-save`) — через stdin, JSON или как есть.
- Выход: в stdout ровно один JSON-объект и перевод строки, код `0`.
- Ошибка: `{"error": "текст для человека", "kind": "invalid|not_found|unavailable"}`
  и код `1`. Платформа выбирает HTTP-код по `kind`, не по тексту.
- Версия контракта: `bridge version` → `{"bridge_api": N}`; платформа
  сверяет `N` с `EXPECTED_BRIDGE_API` и при расхождении показывает баннер
  вместо мёртвых страниц. Меняешь формат ответа команды — поднимай
  `BRIDGE_API` в `bridge.py` и `EXPECTED_BRIDGE_API` вместе.
- Долгие действия (озвучка, сбор новостей, рассылка) мост не делает:
  платформа запускает обычные команды `narrate` как задачи с логом.

## Маршруты

<!-- gate:begin api-routes -->
Маршрутов: 138. Собрано `python3 tools/api_routes.py` — руками не править.

### Общие

Код: `app/platform/aiplatform/server.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/config` | — | JSON | — |
| GET | `/api/status` | — | JSON | — |
| GET | `/api/health` | — | JSON | — |
| GET | `/api/ready` | — | JSON, JSON 503 | — |

### `audio`

Код: `app/platform/aiplatform/audio/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/audio/config` | — | JSON | — |
| POST | `/api/audio/preview` | JSON | JSON | — |
| POST | `/api/audio/split` | JSON | JSON | — |
| GET | `/api/audio/library` | — | JSON | Что можно взять в поле текста: статьи с текстом для озвучки и дайджесты |
| GET | `/api/audio/library/{item_id}` | — | JSON | — |
| GET | `/api/audio/jobs` | — | JSON | — |
| POST | `/api/audio/jobs` | JSON | JSON 201 | — |
| POST | `/api/audio/jobs/batch` | JSON | JSON 201 | Статьи одного файла — в очередь по порядку, каждая своей задачей с общими настройками |
| GET | `/api/audio/jobs/{job_id}` | — | JSON | — |
| POST | `/api/audio/jobs/{job_id}/cancel` | — | JSON | — |
| GET | `/api/audio/jobs/{job_id}/audio` | ?speed, ?download | файл | — |
| GET | `/api/audio/voices` | — | JSON | — |
| GET | `/api/audio/voices/{stem}/sample` | — | файл | — |
| POST | `/api/audio/voices/{stem}` | JSON | JSON 202 | Перезаписать образец: из Gemini TTS (Charon) или из загруженной записи и её текста |
| GET | `/api/audio/tasks/{task_id}` | — | JSON | — |

### `avatar`

Код: `app/platform/aiplatform/avatar/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/avatar/config` | — | JSON | — |
| GET | `/api/avatar/characters` | — | JSON | — |
| POST | `/api/avatar/characters` | JSON | JSON 201 | — |
| GET | `/api/avatar/characters/{char_id}/image` | — | файл | — |
| POST | `/api/avatar/characters/{char_id}/rename` | JSON | JSON | — |
| POST | `/api/avatar/characters/{char_id}/delete` | — | JSON | — |
| GET | `/api/avatar/jobs` | — | JSON | — |
| POST | `/api/avatar/jobs` | JSON | JSON 201 | — |
| GET | `/api/avatar/jobs/{job_id}` | — | JSON | — |
| POST | `/api/avatar/jobs/{job_id}/cancel` | — | JSON | — |
| GET | `/api/avatar/jobs/{job_id}/face` | — | файл | — |
| GET | `/api/avatar/jobs/{job_id}/video` | ?download | файл | — |

### `channels`

Код: `app/platform/aiplatform/channels/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/channels/state` | — | JSON | — |
| POST | `/api/channels/items` | JSON | JSON 201 | — |
| POST | `/api/channels/items/import` | — | JSON | Каналы движка, которых ещё нет в списке, — в channels.json |
| POST | `/api/channels/items/{channel_id}/delete` | — | JSON | — |
| POST | `/api/channels/items/{channel_id}` | JSON | JSON | — |
| POST | `/api/channels/bot/check` | — | JSON | — |
| GET | `/api/channels/subscribers` | — | JSON | — |
| POST | `/api/channels/subscribers/sync` | — | JSON | — |
| GET | `/api/channels/serve` | — | JSON | — |
| POST | `/api/channels/serve/start` | — | JSON | — |
| POST | `/api/channels/serve/stop` | — | JSON | — |

### `library`

Код: `app/platform/aiplatform/library/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/library/state` | — | JSON | — |
| POST | `/api/library/articles` | JSON | JSON 201 | По ссылке — движок скачивает статью; иначе — присланный текст как есть |
| GET | `/api/library/articles/{article_id}` | — | JSON | — |
| POST | `/api/library/articles/{article_id}` | JSON | JSON | — |
| GET | `/api/library/articles/{article_id}/audio/{engine}` | ?download | файл | — |
| POST | `/api/library/articles/{article_id}/{action}` | JSON | JSON 202 | — |
| POST | `/api/library/render` | JSON | JSON 202 | — |
| POST | `/api/library/unsent/{mode}` | — | JSON 202 | `{mode}`: send, mark |
| GET | `/api/library/tasks/{task_id}` | — | JSON | — |
| POST | `/api/library/tasks/{task_id}/cancel` | — | JSON | — |

### `montage`

Код: `app/platform/aiplatform/montage/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/montage/config` | — | JSON | — |
| GET | `/api/montage/rules` | — | JSON | — |
| POST | `/api/montage/rules` | JSON | JSON | — |
| GET | `/api/montage/projects` | — | JSON | — |
| POST | `/api/montage/projects` | JSON | JSON 201 | — |
| GET | `/api/montage/projects/{pid}` | — | JSON | — |
| POST | `/api/montage/projects/{pid}` | JSON | JSON | — |
| POST | `/api/montage/projects/{pid}/sources/{role}` | файл | JSON 201 | — |
| POST | `/api/montage/projects/{pid}/sources/{role}/delete` | — | JSON | — |
| GET | `/api/montage/projects/{pid}/source/{role}` | — | файл | — |
| POST | `/api/montage/projects/{pid}/start` | — | JSON | — |
| POST | `/api/montage/projects/{pid}/replan` | — | JSON | — |
| GET | `/api/montage/projects/{pid}/plan` | — | JSON | — |
| POST | `/api/montage/projects/{pid}/plan` | JSON | JSON | — |
| POST | `/api/montage/projects/{pid}/render` | — | JSON | — |
| POST | `/api/montage/projects/{pid}/revise` | JSON | JSON | — |
| POST | `/api/montage/projects/{pid}/cancel` | — | JSON | — |
| POST | `/api/montage/projects/{pid}/delete` | — | JSON | — |
| GET | `/api/montage/projects/{pid}/video` | ?download | файл | — |
| GET | `/api/montage/projects/{pid}/{name}` | — | файл | — |

### `news`

Код: `app/platform/aiplatform/news/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/news/sources` | — | JSON | — |
| POST | `/api/news/sources/collect` | JSON | JSON 202 | — |
| GET | `/api/news/failed` | — | JSON | — |
| POST | `/api/news/failed/rescue` | JSON | JSON 202 | — |
| GET | `/api/news/top` | ?day | JSON | — |
| GET | `/api/news/reviews` | — | JSON | — |
| POST | `/api/news/reviews/{kind}` | JSON | JSON 202 | `{kind}`: weekly, monthly |
| GET | `/api/news/reviews/{review_id}` | — | JSON | — |
| GET | `/api/news/tasks/{task_id}` | — | JSON | — |
| POST | `/api/news/tasks/{task_id}/cancel` | — | JSON | — |

### `photo`

Код: `app/platform/aiplatform/photo/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/photo/config` | — | JSON | — |
| GET | `/api/photo/jobs` | — | JSON | — |
| POST | `/api/photo/jobs` | JSON | JSON 201 | — |
| GET | `/api/photo/jobs/{job_id}` | — | JSON | — |
| POST | `/api/photo/jobs/{job_id}/cancel` | — | JSON | — |
| POST | `/api/photo/jobs/{job_id}/upscale` | JSON | JSON 201 | — |
| GET | `/api/photo/jobs/{job_id}/image` | ?download | файл | — |

### `settings`

Код: `app/platform/aiplatform/settings/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/settings/state` | — | JSON | — |
| POST | `/api/settings/toggle` | JSON | JSON | {kind: photo\|video\|audio, id, enabled} или {kind: voice, engine, id, enabled} |
| POST | `/api/settings/section/{name}` | JSON | JSON | Часть раздела: {enabled, time, day} расписания, {enabled} уведомлений и т.п. Мусор — 400 |
| GET | `/api/settings/cleanup` | — | JSON | — |
| POST | `/api/settings/cleanup/run` | — | JSON | — |
| POST | `/api/settings/notify-test` | — | JSON | Тестовое уведомление всеми настроенными путями; не дошло ни одним — 502 с причинами |

### `studio`

Код: `app/platform/aiplatform/studio/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/studio/config` | — | JSON | — |
| GET | `/api/studio/projects` | ?scenario | JSON | — |
| POST | `/api/studio/preflight` | JSON | JSON | Проверка и оценка для формы «Новый выпуск» — до создания |
| POST | `/api/studio/stats/refresh` | — | JSON 202 | «Обновить отклик»: подписчики и просмотры в фоне — сеть бывает медленной |
| POST | `/api/studio/projects/{pid}/stats` | JSON | JSON | Отклик, внесённый руками: у пакетов для YouTube, TikTok и у приватных каналов. Кроме просмотров — |
| GET | `/api/studio/health/stats` | — | JSON | «Здоровье» за 7 и 28 дней по журналу событий (REL-010): упавшие шаги, сборка, дайджест вовремя |
| GET | `/api/studio/board` | ?archived | JSON | Все выпуски всех сценариев по группам: ждёт тебя, идёт, опубликовано, черновики |
| POST | `/api/studio/projects/{pid}/split` | JSON | JSON 201 | Серия: по выпуску на часть (по главам, ~8 мин), с теми же настройками. Исходный — в архив |
| POST | `/api/studio/projects/{pid}/summary` | — | JSON 202 | Выжимка: LLM в фоне пишет текст на 5–8 минут, из него — новый выпуск. Ход — в журнале исходного |
| POST | `/api/studio/projects/{pid}/clone` | JSON | JSON 201 | Копия выпуска: те же настройки и готовые результаты до сборки; сборка и публикация — заново |
| POST | `/api/studio/projects/{pid}/archive` | JSON | JSON | — |
| POST | `/api/studio/projects` | JSON | JSON 201 | POST /projects с Idempotency-Key: повтор с тем же ключом отдаёт уже созданный выпуск |
| GET | `/api/studio/projects/{pid}` | — | JSON | — |
| POST | `/api/studio/projects/{pid}/settings` | JSON | JSON | — |
| POST | `/api/studio/projects/{pid}/run` | JSON | JSON 202 | «Выполнить/Перезапустить шаг»: с settings — сначала параметры из карточки шага |
| POST | `/api/studio/projects/{pid}/cancel` | — | JSON | — |
| POST | `/api/studio/projects/{pid}/approve` | JSON | JSON | — |
| POST | `/api/studio/projects/{pid}/publish` | JSON | JSON 202 | Кнопка «Опубликовать», как `make news-publish`: выбранные каналы, повторно — с force |
| POST | `/api/studio/projects/{pid}/publish/resolve` | JSON | JSON | «Пост есть» / «Поста нет» по каналу, где ответа Telegram не было; затем публикация |
| POST | `/api/studio/projects/{pid}/uploaded` | JSON | JSON | «Залил»: автор сам залил пакет на площадку и дал ссылку на ролик — теперь выпуск опубликован |
| POST | `/api/studio/projects/{pid}/artifacts/{name}` | JSON | JSON | — |
| POST | `/api/studio/projects/{pid}/upload/{what}` | файл | JSON | Постер или озвучка от человека; src — загрузка во временном файле (body_to_file) |
| POST | `/api/studio/projects/{pid}/upload/{kind}/{key}` | файл | JSON | Картинка или видео слайда от человека. Все на месте — шаг готов, сборка заново |
| GET | `/api/studio/projects/{pid}/files/{rest…}` | — | файл | — |

### `updates`

Код: `app/platform/aiplatform/updates/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/updates/voice-models` | — | JSON | — |
| POST | `/api/updates/voice-models/{action}` | JSON | JSON 202 | Скачать основные модели, модели mlx для сравнения или перенести модели из ~/.cache; `{action}`: fetch, mlx, move |
| GET | `/api/updates/tasks/{task_id}` | — | JSON | — |
| POST | `/api/updates/tasks/{task_id}/cancel` | — | JSON | — |
| GET | `/api/updates/state` | — | JSON | — |
| GET | `/api/updates/config` | — | JSON | — |
| POST | `/api/updates/models/check` | — | JSON | — |
| POST | `/api/updates/registry/check` | — | JSON | — |
| POST | `/api/updates/registry/apply` | — | JSON | — |
| POST | `/api/updates/registry/dismiss` | — | JSON | — |
| POST | `/api/updates/registry/reset` | — | JSON | — |
| POST | `/api/updates/registry` | JSON | JSON | — |

### `video`

Код: `app/platform/aiplatform/video/service.py`

| Метод | Путь | Тело / query | Ответ | Что делает |
|---|---|---|---|---|
| GET | `/api/video/config` | — | JSON | — |
| GET | `/api/video/jobs` | — | JSON | — |
| POST | `/api/video/jobs` | JSON | JSON 201 | — |
| POST | `/api/video/living` | JSON | JSON 201 | — |
| GET | `/api/video/jobs/{job_id}` | — | JSON | — |
| POST | `/api/video/jobs/{job_id}/cancel` | — | JSON | — |
| GET | `/api/video/jobs/{job_id}/video` | ?download | файл | — |

### Мост `narrate bridge`

Код: `app/narrator/src/narrator/bridge.py`

| Команда | Аргументы | Что отдаёт |
|---|---|---|
| `version` | — | версия контракта моста: bridge_api |
| `engines` | — | движки: голоса, параметры, почему не запустится |
| `voices` | — | образцы голосов-клонов |
| `models` | — | модели озвучки на диске |
| `preview` | — | как прочитает модель текст со stdin |
| `split` | — | статьи markdown-файла со stdin |
| `library` | — | статьи библиотеки со статусами |
| `article-new` | — | статья из JSON со stdin: title, text, url, summary |
| `sources` | — | источники новостей: журнал сбора и польза |
| `failed` | — | источники со сбоем последнего сбора |
| `reviews` | — | обзоры недели и месяца |
| `unsent` | — | озвучки, что не ушли в Telegram |
| `article` | `<id>` | карточка статьи: текст, отчёт проверки, аудио |
| `article-save` | `<id>` | сохранить title, summary, script из JSON со stdin |
| `review` | `<id>` | файлы обзора |
| `fetch` | `<url>` | скачать статью в библиотеку |
| `top` | `[--date]` `[--limit]` | события дня по важности |
| `issue` | `[--date]` | выпуск дня и вердикт судьи |
| `bot` | `[--check]` | токен, имя бота, каналы |
| `subscribers` | `[--sync]` | подписчики бота |
<!-- gate:end api-routes -->
