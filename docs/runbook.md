# Runbook: эксплуатация AI Platform

Что делать, когда живой сервис ведёт себя не так. Устройство и функции — в
[`app/platform/README.md`](../app/platform/README.md), системные ошибки
агентов — в [`грабли.md`](грабли.md).

Все команды — из `app/platform` живого checkout (по умолчанию
`~/apps/aiplatform/app/platform`, см. «Откат версии»), адрес сервиса —
`http://127.0.0.1:8780` (`AIP_PORT`). `<data>` — каталог данных
(`AIP_DATA_DIR`, по умолчанию `~/Library/Application Support/aiplatform`,
до переезда — `app/platform/data`); где он сейчас, печатает `make run` в
строке `данные:` и показывает `/api/ready`.

## Где смотреть

| Что | Где | Команда |
|---|---|---|
| Жив ли процесс | `/api/health` | `curl -s 127.0.0.1:8780/api/health` |
| Готов ли выпустить дайджест | `/api/ready` (200 или 503 + `failed`) | `curl -s 127.0.0.1:8780/api/ready \| python3 -m json.tool` |
| GPU-замок | `/api/status`: `gpu`, `gpu_held_s`, `waiting` | `curl -s 127.0.0.1:8780/api/status` |
| Лог процесса | `<data>/logs/platform.log` (JSON-строки, ротация 10 МБ × 5) | `tail -50 <data>/logs/platform.log` |
| Лог под launchd | `~/Library/Logs/aiplatform/server.log`, `server.err.log`, `digest-check.log`, `deploy.log` | `tail -50 ~/Library/Logs/aiplatform/server.err.log` |
| Уведомления | `<data>/notifications.log`; «Настройки» → последние | `tail <data>/notifications.log` |
| Расписание | `<data>/studio/schedule.json`: последний тик, итог дайджеста | `cat <data>/studio/schedule.json` |
| Процессы моделей | `<data>/run/<pgid>.json` | `ls <data>/run/` |
| Здоровье за 7/28 дней | `/api/studio/health/stats`, «Настройки» → «Здоровье» (по `<data>/events.jsonl`) | `curl -s 127.0.0.1:8780/api/studio/health/stats` |
| Агенты launchd | | `make agent-status` |
| Последний бэкап | `/api/ready`, проверка `backup`; `<data>/backup.json` | |

## 1. Дайджест не вышел

**Симптом.** Утром нет поста в канале; пришло «Дайджест не создан» / «Сервис
недоступен» / «Дайджест упал» от внешнего чекера или «Дайджест по
расписанию не запустился» от платформы.

**Где смотреть.**

```sh
make agent-status                                   # агент загружен? pid? last exit code?
curl -s 127.0.0.1:8780/api/health                   # schedule.enabled, schedule.last
curl -s 127.0.0.1:8780/api/ready | python3 -m json.tool   # schedule, token, last_digest, disk, models_dir
grep -E 'schedule|digest|step.failed|publication' <data>/logs/platform.log | tail -30
```

**Что делать — по тому, что нашли.**

- `/api/health` не отвечает — сервис лежит. `launchctl kickstart -k
  gui/$(id -u)/com.aiplatform.server`; не поднялся за 30 с —
  `tail -50 ~/Library/Logs/aiplatform/server.err.log` (типично: Python не
  тот, `local.mk` с ошибкой, порт занят — `lsof -i :8780`).
- `schedule.enabled: false` — расписание выключено во «Настройках».
- `ready.schedule` не ок — поток расписания не тикал: перезапуск сервиса
  (как выше). Mac спал в момент дайджеста — `pmset -g sched`, будить:
  `sudo pmset repeat wakeorpoweron MTWRFSU 06:55:00`.
- `schedule.last.result: failed` — запуск не создал выпуск (narrator, claude,
  сеть): текст в `schedule.last.error` и `notifications.log`. Платформа
  повторяет сама (до трёх попыток за день); после — «Запустить» на странице
  дайджеста (`/studio/?scenario=digest`).
- Выпуск создан, но стоит: открыть его на доске (`/board/`) — шаг с ошибкой
  или «ждёт тебя» (обложка из ChatGPT, вердикт судьи «не готово»). Поправить
  и «Перезапустить шаг» / «Опубликовать».
- `ready.token` не ок — см. сценарий 3.
- `ready.disk` / `ready.models_dir` не ок — см. сценарий 4.

**Проверка.** Пост в канале; `curl -s 127.0.0.1:8780/api/ready` → `"ok":
true`, `last_digest` — сегодняшний день без `failed`; `make digest-check`
молчит (код выхода 0).

## 2. Выпуск завис, GPU занят

**Симптом.** Шаг выпуска давно «Идёт» без прогресса; в шапке «GPU: …»
держится долго, задачи других вкладок «Ждут GPU».

**Где смотреть.**

```sh
curl -s 127.0.0.1:8780/api/status          # gpu (кто держит), gpu_held_s, waiting
ls <data>/run/ && cat <data>/run/*.json    # процессы моделей: pgid, argv, started_at
ps -o pid,etime,rss,args -g <pgid>         # жив ли процесс, сколько памяти
grep -E 'process.timeout|task.failed|step.failed' <data>/logs/platform.log | tail
```

**Что делать.**

1. Отменить штатно: «Остановить» на странице выпуска или «Отменить» у
   задачи во вкладке (Фото / Видео / Аудио) — гасится вся группа процессов.
2. Не помогло за минуту — `kill -TERM -<pgid>` (минус — вся группа), затем
   `kill -KILL -<pgid>`.
3. Сервис сам не отвечает — `launchctl kickstart -k
   gui/$(id -u)/com.aiplatform.server`: при остановке процессы гасятся, при
   старте платформа гасит сирот по `<data>/run/`. Шаг выпуска станет
   «Сервис был перезапущен во время шага»; выпуск, шедший сам, продолжит
   шаг один раз (публикация сама не повторяется).
4. Зависает регулярно — сузить лимит времени этого вида процессов в
   `<data>/settings.json`: `"timeouts": {"video": {"total": 60, "idle": 15}}`
   (минуты; виды: llm, image, render, video, audio, news). Процесс без
   вывода дольше `idle` снимается сам.

**Проверка.** `curl -s 127.0.0.1:8780/api/status` → `"gpu": null`,
`waiting: 0`; `ls <data>/run/` пуст; шаг выпуска перезапускается и идёт.

## 3. Telegram не принимает

**Симптом.** Шаг «Публикация» упал или отметил канал «Статус неизвестен —
проверь канал»; уведомления владельцу не приходят; `ready.token` не ок.

**Где смотреть.** Ошибка шага на странице выпуска (текст ответа Telegram),
`<data>/studio/projects/<id>/published.json` (по каналу: `sending` / `sent` /
`unknown` / `failed`), `grep -E 'publication|notify' <data>/logs/platform.log`.

**Что делать — по ответу.**

- **Токен** («нет токена бота», «Unauthorized», `ready.token`): токен ищется
  в переменной из `token_env` канала (`channels.json`), иначе
  `NARRATOR_TELEGRAM_TOKEN`, иначе файл `NARRATOR_TELEGRAM_TOKEN_FILE` /
  `app/narrator/bot_token.md` (у живого — в `AIP_NARRATOR_DIR`). Под launchd
  `.zshrc` не читается: переменная должна быть в `local.mk` или файле.
  Перевыпущенный в BotFather токен — записать туда же и перезапустить
  сервис. Значение токена в чат, лог и коммиты не писать.
- **429 Too Many Requests: retry after N** — подождать N секунд и нажать
  «Опубликовать» без «повторно»: уйдёт только в каналы, куда ещё не ушло.
- **Видео больше 50 МБ** («Bot API такое не примет»): пересобрать с
  `AIP_RENDER_SCALE=0.75` (или короче озвучка), либо отправить через пакет
  для ручной загрузки (`publish/<канал>/` в каталоге выпуска).
- **«Статус неизвестен»** — ответ не дошёл (сеть, рестарт). Открыть канал:
  пост есть — «Отметить отправленным», нет — «Отправить заново». Без
  проверки не повторять: будет дубль.
- **Chat not found / Forbidden** — бот не админ канала или неверный
  `chat_id` во вкладке «Каналы».

**Проверка.** Пост в канале; в `published.json` канал в `sent` с
`message_id`; «Настройки» → «Проверить уведомление» доходит; `ready.token` ок.

## 4. Кончился диск или не примонтирован SSD

**Симптом.** Задачи падают с «…свободно X ГБ на диске данных, нужно от Y
ГБ»; пришло уведомление «Мало места на диске»; `ready.disk` не ок; баннер
«Каталог моделей … не найден — внешний диск не примонтирован?»;
`ready.models_dir` не ок.

**Где смотреть.**

```sh
curl -s 127.0.0.1:8780/api/ready | python3 -m json.tool   # disk, models_dir
df -h "<data>" "/Volumes/External SSD"
curl -s 127.0.0.1:8780/api/settings/cleanup | python3 -m json.tool   # место по областям и что уберётся
du -sh <data>/* | sort -h | tail
```

**Что делать.**

- **Место.** «Настройки» → «Уборка» → «Убрать сейчас» (или `curl -s -X
  POST 127.0.0.1:8780/api/settings/cleanup/run`): задачи вкладок, кэш,
  загрузки и архив старше срока, промежуточные файлы опубликованных
  выпусков. Мало — уменьшить `cleanup.days` / `trim_days` в
  `<data>/settings.json` и повторить; убрать в архив ненужные выпуски на
  доске. Чужое место: `~/.cache/huggingface`, `~/Library/Caches`, корзина.
- Порог проверки — `cleanup.min_free_gb` (10 ГБ) в `<data>/settings.json`;
  `/api/ready` смотрит на `AIP_MIN_FREE_GB` (5 ГБ).
- **SSD.** Подключить диск, проверить `ls "/Volumes/External SSD/local_models"`.
  Диск переименован или веса переехали — `AIP_MODELS_DIR=…` в `local.mk` и
  перезапуск сервиса. Модели озвучки — `NARRATOR_MODELS_DIR`.

**Проверка.** `/api/ready`: `disk` и `models_dir` ок; баннер «Проблемы»
пуст; упавший шаг «Перезапустить шаг» проходит.

## 5. Откат версии и восстановление из бэкапа

**Откат версии.** Новая версия сломала выпуск или публикацию.

```sh
make deployed                      # какой тег сейчас у живого checkout (теги ставит make release-tag в main)
tail ~/Library/Logs/aiplatform/deploy.log   # история: было → стало
make deploy TAG=<прошлый тег>      # бэкап → checkout тега → перезапуск
```

Выпуски, созданные новой версией, старый код может не прочитать (`project.json`
мигрирует только вперёд): такие выпуски не трогать до возврата на новую
версию; данные — из бэкапа, сделанного `make deploy` перед обновлением.

**Восстановление из бэкапа.** Данные испорчены или потеряны (`git clean`,
смерть диска, новая машина).

```sh
ls -t "$AIP_BACKUP_DIR"/aiplatform-*.tar.gz.age | head -3       # свежие архивы
make restore FILE="<архив>" TO=~/aiplatform-restore            # только в пустой каталог
launchctl bootout gui/$(id -u)/com.aiplatform.server           # остановить живой сервис
mv "<data>" "<data>.broken" && cp -R ~/aiplatform-restore/data "<data>"
cp -R ~/aiplatform-restore/narrator-library/. "<библиотека narrator>/"   # $NARRATOR_LIBRARY или <AIP_NARRATOR_DIR>/library; без mp3
cp ~/aiplatform-restore/platform/local.mk .                     # если потерян
make install-agent
curl -s 127.0.0.1:8780/api/ready | python3 -m json.tool
```

Ключ расшифровки — `AIP_BACKUP_AGE_IDENTITY` (по умолчанию
`~/.config/aiplatform/backup-key.txt`); без него архив не открыть — копия
ключа хранится вне этого Mac (менеджер паролей). Медиа (mp3, видео) в
бэкап не входят: опубликованное есть в каналах, озвучку можно пересобрать.

**Проверка.** `/api/ready` → `ok: true`; на доске те же выпуски, во
вкладке «Каналы» — те же каналы и подписчики бота; `make deployed`
показывает ожидаемый тег.

## Конфигурация

Четыре места: `local.mk` (переменные окружения, рядом с Makefile, не в
git), `<data>/settings.json` (вкладка «Настройки» и ручная правка),
`channels.json` (каналы публикации), файлы narrator (токен бота и каналы
статей). Под launchd окружение из `.zshrc` не приходит: всё, что нужно
сервису, — в `local.mk` (его подхватывает `make run` из plist) или в
файлах. Значения секретов здесь и в логах не выписываются.

### Переменные `AIP_*`

| Переменная | Кто читает | По умолчанию | Под launchd |
|---|---|---|---|
| `AIP_HOST`, `AIP_PORT` | `config.py` | `127.0.0.1`, `8780` | по желанию |
| `AIP_AUTH_TOKEN` | `config.py`, сервер | — (без него не-loopback `AIP_HOST` не стартует) | только если сервис открыт в сеть; значение — в `local.mk` |
| `AIP_ALLOWED_HOSTS` | `config.py`, сервер | — | свои имена в `Host` через запятую |
| `AIP_DATA_DIR` | `config.py` | `~/Library/Application Support/aiplatform`; нет его, но есть `app/platform/data` — старый путь | по желанию |
| `AIP_BACKEND` | `config.py` | `real` (`fake` — `make demo`) | не задавать |
| `AIP_FFMPEG` | `config.py` | `ffmpeg` из `PATH` | `PATH` собирает `make install-agent` |
| `AIP_FONT` | `config.py` | Arial Unicode из `/System/Library/Fonts` | если шрифта нет |
| `AIP_MODELS_DIR` | фото, видео | `/Volumes/External SSD/local_models` | да, если не SSD |
| `AIP_VIDEO_MODELS_DIR`, `AIP_VIDEO_MODEL`, `AIP_VIDEO_LTX_CMD`, `AIP_VIDEO_WAN_CMD`, `AIP_VIDEO_WAN_STEPS` | видео | из `PATH` / рядом с моделью / `10` | пишет `make models` |
| `AIP_PHOTO_MFLUX_BIN`, `AIP_PHOTO_FLUX2_CMD` | фото | из `PATH` | пишет `make models` |
| `AIP_LOW_RAM` | фото, видео | — | по желанию |
| `AIP_NARRATOR_DIR`, `AIP_NARRATE_CMD`, `AIP_AUDIO_ENGINE` | аудио, движок | `app/narrator`, `<dir>/narrate`, первый с весами | да, если narrator не в репо |
| `AIP_LLM_CMD`, `AIP_UPDATES_LLM_CMD` | сценарии, «Обновления» | `claude -p …` (изоляция без инструментов) | полный путь к `claude`, если не в `PATH` |
| `AIP_NEWS_CMD`, `AIP_NEWS_WHERE` | дайджест | `make -C {narrator} news-…` | нет |
| `AIP_STUDIO_IMAGE_MODEL`, `AIP_STUDIO_STICKER`, `AIP_STUDIO_PLAY` | сценарии | `z-image-turbo`, ассеты студии | нет |
| `AIP_CHANNELS` | сценарии | `<data>/channels.json`, иначе `app/platform/channels.json` | нет |
| `AIP_RENDER_SCALE` | сборка видео | `1` | нет |
| `AIP_NOTIFY_CMD` | уведомления | osascript на macOS | по желанию |
| `AIP_SCHEDULE_TICK_S` | расписание | `30` (`0` — выключено) | нет |
| `AIP_HEARTBEAT_URL` | расписание | — | по желанию (healthchecks.io) |
| `AIP_MIN_FREE_GB` | `/api/ready` | `5` | нет |
| `AIP_LOG_STDERR` | лог процесса | — | `1` ставит plist |
| `AIP_DIGEST_DEADLINE` | «Здоровье»: дайджест вовремя | `12:00` | нет |
| `AIP_BACKUP_DIR`, `AIP_BACKUP_AGE_RECIPIENT` / `AIP_BACKUP_AGE_RECIPIENTS_FILE`, `AIP_BACKUP_AGE_IDENTITY`, `AIP_BACKUP_KEEP_DAYS`, `AIP_BACKUP_AGE` | `make backup` / `restore` | —, —, `~/.config/aiplatform/backup-key.txt`, `30`, `age` | да: `AIP_BACKUP_DIR` и ключ — в `local.mk` |
| `AIP_LIVE_DIR`, `AIP_RELEASE_REMOTE`, `AIP_DEPLOY_BACKUP`, `AIP_DEPLOY_NO_BACKUP` | `make deploy` (`tools/deploy.sh`) | `~/apps/aiplatform`, `origin`, `make backup`, — | нет |
| `AIP_CHECK_AT`, `AIP_CHECK_URL`, `AIP_CHECK_GRACE_MIN`, `AIP_ALERT_TG_TOKEN`, `AIP_ALERT_TG_CHAT` | внешний чекер | время дайджеста + 30 мин, `http://127.0.0.1:8780`, `30`, Keychain | токен и чат — в Keychain |
| `AIP_AGENTS_DIR`, `AIP_LOG_DIR`, `AIP_AGENT_LABEL`, `AIP_CHECK_LABEL`, `AIP_BACKUP_LABEL`, `AIP_BACKUP_AT` | `tools/agent.sh` | `~/Library/LaunchAgents`, `~/Library/Logs/aiplatform`, метки `com.aiplatform.*`, `03:30` | нет |
| `AIP_TEST_GUARD` | тесты | — | никогда |
| `AIP_TELEGRAM_TOKEN`, `AIP_TG_<ИМЯ>` (имя — из `token_env` канала) | публикация | — | в `local.mk`, не в git |

Переменные narrator: `NARRATOR_TELEGRAM_TOKEN`, `NARRATOR_TELEGRAM_TOKEN_FILE`
(`bot_token.md`), `NARRATOR_TELEGRAM_CHANNEL`, `NARRATOR_LIBRARY`
(библиотека: по умолчанию `<AIP_NARRATOR_DIR>/library` основного checkout),
`NARRATOR_MODELS_DIR`, `NARRATOR_ENGINE` — их читает движок (README
движка).

### Файлы

| Файл | Где | Кто пишет | Что в нём |
|---|---|---|---|
| `local.mk` | `app/platform/` (живого checkout) | человек, `make models` | `AIP_*` |
| `settings.json` | `<data>/` | «Настройки», ручная правка | выключенное, расписание, уведомления, уборка и порог места, лимиты времени |
| `channels.json` | `<data>/` или `app/platform/` | «Каналы» | каналы публикации, имя переменной токена |
| `schedule.json` | `<data>/studio/` | расписание | последний тик и итог дайджеста |
| `backup.json` | `<data>/` | `make backup` | время и файл последнего бэкапа |
| `events.jsonl` | `<data>/` | выпуски, публикации, отклик | журнал событий (AN-002): по нему считается «Здоровье» |
| `bot_token.md`, `telegram_tems.md` | `AIP_NARRATOR_DIR` | человек | токен бота (секрет), каналы статей |
| `library/telegram.json` | библиотека narrator | бот | подписчики, курсор `getUpdates` |
| Keychain `aiplatform-digest-check` | Keychain входа | человек | токен и чат внешнего чекера |

## Ограничения

- **Один процесс, один владелец.** Состояние — в памяти процесса и JSON-файлах
  на диске, без базы; расписание — поток внутри процесса. Двух экземпляров
  на одних данных быть не должно; пользователей и прав нет — сервис слушает
  только `127.0.0.1`.
- **macOS и Apple Silicon.** Модели — MLX (mflux, Flux2CLI, ltx-2-mlx, Wan),
  озвучка — MLX/Torch на MPS; launchd, `osascript`, `pmset`, Keychain,
  `tmutil`; шрифты из `/System/Library/Fonts`. На Linux не работает генерация
  и автозапуск; без моделей работает `make demo`.
- **`claude -p` с логином пользователя.** LLM-шаги зовут Claude Code CLI под
  учёткой владельца Mac: истёк логин — падают промпты и дайджест.
- **Внешний SSD.** Веса (сотни ГБ) — на `/Volumes/External SSD`; не
  примонтирован — генерация недоступна.
- **GPU-замок.** Тяжёлые модели идут по одной на машину; длинное видео
  держит очередь.
- **Telegram Bot API.** Файлы до 50 МБ; лимиты частоты (429).

При переезде на сервер придётся заменить: MLX-модели — на CUDA-аналоги или
внешние API; launchd — на systemd; osascript и Keychain — на свой канал
уведомлений и хранилище секретов; `claude -p` — на API-ключ; JSON-файлы и
поток расписания — на базу и отдельный планировщик, если экземпляров больше
одного; привязку к `127.0.0.1` — на авторизацию.
