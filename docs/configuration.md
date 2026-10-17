# Настройка AI Platform

Полный справочник конфигурации сервиса `app/platform`: доступ и защита,
все переменные окружения, лог процесса, остановка и лимиты времени,
автозапуск, данные и бэкап, обновление и откат. Коротко
и с командами — [README сервиса](../app/platform/README.md#настройка);
эксплуатация живого сервиса — [runbook](runbook.md).

## Содержание

- [Доступ и защита](#доступ-и-защита)
- [Переменные окружения](#переменные-окружения)
- [Лог, остановка, лимиты времени](#лог-остановка-лимиты-времени)
- [Автозапуск: подробности](#автозапуск-подробности)
- [Данные и бэкап](#данные-и-бэкап)
- [Обновление и откат](#обновление-и-откат)

## Доступ и защита

Аутентификации у платформы нет: кто достучался до порта, тот может публиковать
в каналы, запускать агентов и удалять данные. Поэтому по умолчанию сервер
слушает только `127.0.0.1`, а запросы проверяются так:

- **Host** — только `127.0.0.1:<порт>`, `localhost:<порт>`, `[::1]:<порт>`,
  адрес из `AIP_HOST` и `AIP_ALLOWED_HOSTS`; чужое имя (DNS rebinding) — 421;
- **POST** — с `Origin` из того же списка или с заголовком `X-AIP: 1` (его
  ставит `ui.js api()`); иначе 403. Скрипты и `curl` шлют `-H 'X-AIP: 1'`;
- во всех ответах `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer` и `Content-Security-Policy` (только свои
  скрипты, без встраивания в чужие фреймы).

**Удалённый доступ** — не открывай порт наружу. Ходи через SSH-туннель
(`ssh -L 8780:127.0.0.1:8780 мак`, затем http://127.0.0.1:8780) или Tailscale
(`tailscale serve 8780`). Если всё же слушать сеть (`AIP_HOST=0.0.0.0` или
адрес интерфейса), нужен `AIP_AUTH_TOKEN` — без него сервер не стартует:

```sh
export AIP_AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
export AIP_HOST=0.0.0.0 AIP_ALLOWED_HOSTS=box.local
```

Вход — по ссылке `http://box.local:8780/?token=<токен>`: сервер ставит cookie
(HttpOnly, SameSite=Strict) и уводит на адрес без токена; скрипты шлют
`Authorization: Bearer <токен>`. Без токена — 401 (страницы показывают форму
входа). `GET /api/health` и `GET /api/ready` отвечают без токена.

## Переменные окружения

Переменные окружения — в `local.mk` рядом с Makefile (не в git) или в
окружении. Пустое значение — значение по умолчанию. Готовый пример с путями,
которые ставит `make models`: `cp local.mk.example local.mk`.

| Переменная | По умолчанию | Что это |
|---|---|---|
| `AIP_PORT`, `AIP_HOST` | `8780`, `127.0.0.1` | где слушать |
| `AIP_AUTH_TOKEN` | — | токен доступа; без него сервер на не-loopback адресе не стартует |
| `AIP_ALLOWED_HOSTS` | — | свои имена в Host через запятую (`box.local,10.0.0.5:8780`) |
| `AIP_DATA_DIR` | `~/Library/Application Support/aiplatform`; пока его нет, а `app/platform/data` есть — старый путь | задачи, загрузки, выпуски, настройки, каналы (вне рабочей копии — см. [«Данные и бэкап»](#данные-и-бэкап)) |
| `AIP_MODELS_DIR` | `/Volumes/External SSD/local_models` (у автора — внешний диск) | веса фото- и видеомоделей; диска нет — свой каталог, `~/local_models`, см. [«Если не работает при установке»](../app/platform/README.md#если-не-работает-при-установке) |
| `AIP_LOW_RAM` | — | экономия памяти для фото и видео |
| `AIP_PHOTO_MFLUX_BIN`, `AIP_PHOTO_FLUX2_CMD` | из PATH | пайплайны фото (или старые `PHOTOGEN_*`) |
| `AIP_VIDEO_LTX_CMD`, `AIP_VIDEO_WAN_CMD`, `AIP_VIDEO_MODEL` | из PATH | пайплайны видео (или старые `VIDEOGEN_*`) |
| `AIP_NARRATOR_DIR` | `app/narrator` | движок озвучки и новостей |
| `AIP_NARRATE_CMD` | `$AIP_NARRATOR_DIR/narrate` | команда озвучки (`narrate say`) |
| `AIP_AUDIO_ENGINE` | первый с весами: `higgs` → `espeech` → `silero` | движок озвучки по умолчанию; не задан — первый, чьи веса стоят (после одного `make models` — `espeech`, higgs ставит `make models-mlx E=higgs`). Нет весов у движка по умолчанию — баннер «Проблемы» с командой установки |
| `AIP_LLM_CMD` | `claude -p --tools "" --setting-sources project --strict-mcp-config` | LLM для промптов и слайдов: промпт на stdin, ответ в stdout. Без инструментов, MCP и пользовательских настроек: модель читает чужой текст, prompt injection не должен дотянуться до файлов и сети |
| `AIP_UPDATES_LLM_CMD` | `claude -p --tools WebSearch,WebFetch --setting-sources project --strict-mcp-config --settings '{"permissions": …}'` | LLM со свежим вебом для сверки списка запрещённых организаций: только поиск и чтение веба |
| `AIP_NEWS_CMD` | `make -C {narrator} news-collect news-agent news-judge DAY={day}` | сбор дайджеста |
| `AIP_NEWS_WHERE` | `{narrator}/narrate news where --date {day}` | где лежит выпуск |
| `AIP_STUDIO_IMAGE_MODEL` | `z-image-turbo` | модель постера и картинок по умолчанию |
| `AIP_STUDIO_STICKER`, `AIP_STUDIO_PLAY` | `aiplatform/studio/assets/subscribe.png`, `play.png` | плашка «подпишись» и кнопка «плей» (PNG с прозрачностью) |
| `AIP_CHANNELS` | `<data>/channels.json`; нет его, а есть `app/platform/channels.json` — старый | каналы публикации; нет файла — каналы из `telegram_tems.md` narrator |
| `AIP_BACKUP_DIR` | — | куда `make backup` кладёт архивы (iCloud Drive, второй диск); задан — `make install-agent` ставит ежедневный бэкап |
| `AIP_BACKUP_AGE_RECIPIENT` или `AIP_BACKUP_AGE_RECIPIENTS_FILE` | — | открытый ключ `age` (`age1…`) или файл с ключами, на которые шифруется бэкап |
| `AIP_BACKUP_AGE_IDENTITY` | `~/.config/aiplatform/backup-key.txt` | закрытый ключ для `make restore` |
| `AIP_BACKUP_KEEP_DAYS`, `AIP_BACKUP_AT`, `AIP_BACKUP_AGE` | `30`, `03:30`, `age` | срок хранения архивов, время ежедневного бэкапа, путь к `age` |
| `AIP_LIVE_DIR`, `AIP_RELEASE_REMOTE` | `~/apps/aiplatform`, `origin` | живой checkout для `make deploy`; remote для тегов |
| `AIP_FONT` | системный Arial Unicode | шрифт слайдов с кириллицей |
| `AIP_FFMPEG` | `ffmpeg` | путь к ffmpeg |
| `AIP_RENDER_SCALE` | `1` | доля от 1920×1080 (0.5 — быстрые черновики) |
| `AIP_NOTIFY_CMD` | — | своя команда уведомлений (заголовок и текст — на stdin); без неё на Mac — osascript |
| `AIP_SCHEDULE_TICK_S` | `30` | как часто расписание смотрит на часы; `0` — расписание выключено |
| `AIP_MIN_FREE_GB` | `5` | меньше свободного на диске данных — `/api/ready` отвечает 503 |
| `AIP_DIGEST_DEADLINE` | `12:00` | «Здоровье»: дайджест вовремя, если ушёл в канал до этого времени дня создания |
| `AIP_LOG_STDERR` | — (`1` в LaunchAgent) | дублировать лог процесса в stderr |
| `AIP_BACKEND` | `real` | `real` — настоящие модели; `fake` — фейковые генераторы, TTS, LLM и новости (так запускает `make demo`) |
| `AIP_VIDEO_MODELS_DIR` | `AIP_MODELS_DIR`, иначе каталог рядом с `AIP_VIDEO_MODEL` | веса видеомоделей, если они лежат отдельно от фото (или старый `VIDEOGEN_MODELS_DIR`) |
| `AIP_VIDEO_WAN_STEPS` | `10` | шаги Wan 2.2 (или старый `VIDEOGEN_WAN_STEPS`); целое от 1 |
| `AIP_TEST_GUARD` | — | ставят тесты (`app/platform/tests/__init__.py`): с ним сервис отказывается работать на боевых `data/` и `data-demo/` |
| `AIP_TELEGRAM_API` | `https://api.telegram.org` | адрес Bot API для публикации и уведомлений; тесты ставят фейковый сервер `tests/fake_telegram.py`. Токен уходит по этому адресу — в бою не задавать |
| `NARRATOR_ENGINE` | — | запасное имя для `AIP_AUDIO_ENGINE`; его же читает Makefile движка |
| `NARRATOR_MODELS_DIR` | `/Volumes/External SSD/local_models` (у автора — внешний диск) | папка моделей озвучки; читает движок `app/narrator`; диска нет — свой каталог |
| `AIP_TELEGRAM_TOKEN` (или другое имя) | — | токен бота канала: имя переменной задаёт `token_env` в `channels.json`, см. [«Каналы публикации»](../app/platform/README.md#каналы-публикации); значение не в git |
| `NARRATOR_TELEGRAM_TOKEN` | — | токен бота движка для каналов без своего `token_env`; значение не в git |
| `NARRATOR_TELEGRAM_TOKEN_FILE` | `app/narrator/bot_token.md` | файл с токеном бота, если переменная выше пуста |
| `NARRATOR_BOT_MAX_SUBSCRIBERS` | `1000` | бот не подписывает сверх этого числа; `0` — без ограничения |
| `NARRATOR_BOT_INVITE` | — | инвайт-код: подписка только по `/start <код>` (ссылка `t.me/<бот>?start=<код>`) |
| `NARRATOR_BOT_REPLY_INTERVAL` | `60` | секунд между ответами бота одному чату (защита от флуда) |
| `NARRATOR_TELEGRAM_CHANNEL` | канал статей из `telegram_tems.md` | `off` — озвучка статей идёт подписчикам бота в личку, а не в канал |
| `FAKE_NARRATOR_HOME`, `FAKE_NARRATOR_DELAY`, `FAKE_TTS_STEP_DELAY`, `FAKE_TTS_WORDS_PER_SECOND`, `FAKE_TTS_FAIL` | внутри данных платформы, `0.05`, `0.3`, `2.5`, — | только для демо и тестов: каталог фейкового движка, паузы шагов, темп фейковой речи (короче аудио — быстрее рендер в тестах), падение озвучки на середине |

Имена `NARRATOR_CHANNELS`, `NARRATOR_COVER`, `NARRATOR_COVER_EXT`,
`NARRATOR_FOLDER`, `NARRATOR_PROMPT`, `NARRATOR_REVIEW`,
`NARRATOR_TOKEN_ENV`, `NARRATOR_TOKEN_FILE`, `NARRATOR_TOKEN_FILE_ENV` в
коде — константы (имена файлов и переменных выше), а не переменные
окружения: задавать их не нужно.

Старые `local.mk` от `setup-model.sh` прежних репозиториев photogen и videogen подходят как есть:
платформа читает их `PHOTOGEN_*` / `VIDEOGEN_*`, если `AIP_*` не заданы.

## Лог, остановка, лимиты времени

**Лог процесса** — `data/logs/platform.log`, JSON-строка на событие
(`{ts, level, event, project, step, task, duration_s, error}`), ротация
10 МБ × 5: старт и стоп, тики расписания, задачи и шаги выпусков с
длительностью, публикации (канал, `message_id`), уборка, таймауты,
уведомления, необработанные ошибки HTTP и запросы, кроме чтения (опросы
`/api/status` не пишутся). «Почему вчера не вышел дайджест»:
`grep -E 'schedule|step.failed|publication' data/logs/platform.log`.

**Остановка.** SIGTERM (launchd, `kill`, перезагрузка): новые POST получают
503, процессы моделей гасятся всей группой, сервис выходит за ≤15 с. Пока
процесс модели жив, его pid лежит в `data/run/`; процессы, пережившие
аварийный конец платформы, гасятся при следующем старте. Шаг выпуска,
прерванный остановкой, после рестарта даёт уведомление «Выпуск прерван
перезапуском», а выпуск, который шёл сам (расписание, «Запустить всё»), один
раз продолжает этот шаг; публикация сама не повторяется.

**Лимиты времени.** Зависший процесс снимается по таймауту и отпускает
GPU-замок: LLM — 10 мин, картинка — 15, сборка видео — 30, видео — 60,
озвучка — 90, сбор новостей — 30. Свои значения (минуты, `total` — весь
запуск, `idle` — без вывода) — в `data/settings.json`:
`"timeouts": {"video": {"total": 120, "idle": 20}}`.

## Автозапуск: подробности

Установка — `make install-agent` (README сервиса, «Автозапуск»). Как это работает:

- Сервис запускается как `make run` (берёт `local.mk`), `PATH` с brew, uv и
  claude подставляется при установке: launchd не читает `.zshrc`. Поставил
  новую команду в другой каталог — `make install-agent` ещё раз.
- Упал или убит (`kill -9`) — launchd поднимет его через ≤30 с
  (`ThrottleInterval`). Штатный выход с кодом 0 не перезапускается.
- Перезапуск после обновления кода: `launchctl kickstart -k gui/$(id -u)/com.aiplatform.server`.
- Логи: `data/logs/platform.log` (под launchd он же — в
  `~/Library/Logs/aiplatform/server.err.log`), вывод старта —
  `server.log`, чекер — `digest-check.log`.
- Mac спит в 07:00 — расписание не сработает. Будить его к выпуску:
  `sudo pmset repeat wakeorpoweron MTWRFSU 06:55:00` (снять — `sudo pmset repeat cancel`).

**Внешний чекер дайджеста** `com.aiplatform.digest-check` запускается раз в
день: время дайджеста из настроек + 30 мин, по умолчанию 09:00; своё —
`AIP_CHECK_AT=ЧЧ:ММ make install-agent`. Сервис не ответил — «Сервис
недоступен», расписание включено, а дайджеста за день нет — «Дайджест не
создан», выпуск упал — «Дайджест упал». Поменял время дайджеста —
`make install-agent` заново, чтобы сдвинулся чекер.

По желанию — heartbeat-сервис (healthchecks.io): `AIP_HEARTBEAT_URL=<url>` в
`local.mk`, платформа пингует его после каждого тика расписания, и сервис сам
напишет, если пинги пропали.

## Данные и бэкап

Данные живут вне рабочей копии — в `~/Library/Application Support/aiplatform`
(`AIP_DATA_DIR`): чистка рабочей копии, ветки и worktree агента их не
трогают. Пока этого каталога нет, а старый `app/platform/data` есть,
платформа работает со старым. Перенос — один раз, в обычном терминале:

```sh
make migrate-data          # копия data/ (и channels.json) в новый каталог; старое не удаляется
launchctl kickstart -k gui/$(id -u)/com.aiplatform.server
```

Проверь доску и каналы — после этого `app/platform/data` можно удалить.
Библиотека narrator (статьи, база новостей, подписчики бота) лежит в
`<AIP_NARRATOR_DIR>/library` (сейчас — `~/my_track/07_offline_text-to-word/library`)
или в `$NARRATOR_LIBRARY`.

**Бэкап** — маленькие невосстановимые файлы: настройки, каналы,
расписание, `project.json` / `published.json` / тексты выпусков, журнал
запусков, `local.mk`, библиотека narrator без mp3/wav/mp4 и состояние бота
(`telegram.json`). Без медиа, весов, задач вкладок и кэша; токен бота не
кладётся. Архив шифруется [`age`](https://age-encryption.org) и кладётся в
`AIP_BACKUP_DIR`; архивы старше 30 дней удаляются. Один раз:

```sh
brew install age
mkdir -p ~/.config/aiplatform && age-keygen -o ~/.config/aiplatform/backup-key.txt
# копию ключа — в менеджер паролей: без него архив не открыть
# в local.mk:
#   AIP_BACKUP_DIR=$(HOME)/Library/Mobile Documents/com~apple~CloudDocs/aiplatform-backups
#   AIP_BACKUP_AGE_RECIPIENT=age1…   (строка «public key» из age-keygen)
make backup                # первый архив; потом — каждый день агентом
make install-agent         # ставит и com.aiplatform.backup (03:30, AIP_BACKUP_AT)
make restore FILE=<архив> TO=~/aiplatform-restore   # только в пустой каталог
```

Возраст последнего бэкапа — в `/api/ready` (проверка `backup`; не настроен —
не мешает, старше 48 ч — 503). Восстановление на живой сервис — в
[runbook](runbook.md), сценарий 5.

Тяжёлое и восстановимое — веса моделей, кэш генераций, `.venv` движка, кэш
Hugging Face — исключить из Time Machine: `make tm-exclude` (`tmutil
addexclusion` по каждому пути).

## Обновление и откат

Живой сервис работает не из рабочей копии (там агент делает ветки), а из
отдельного checkout на теге релиза: `~/apps/aiplatform` (`AIP_LIVE_DIR`).
Один раз:

```sh
git clone <url репозитория> ~/apps/aiplatform
cp app/platform/local.mk ~/apps/aiplatform/app/platform/     # local.mk не в git
make -C ~/apps/aiplatform/app/platform uninstall-agent install-agent   # агент запомнит живой каталог
```

После слияния PR в `main` — человек, из любого checkout `app/platform`:

```sh
make release-tag           # в main: тег v<версия> (версия-дата, DX-009), затем git push origin <тег>
make deploy TAG=v2026.10.17   # бэкап → git fetch → checkout тега в живом checkout → перезапуск агента
make deployed              # какой тег сейчас у живого сервиса
```

**Откат** — тот же `make deploy TAG=<прошлый тег>` (его печатает каждый
деплой, история — `~/Library/Logs/aiplatform/deploy.log`). Выпуски
(`project.json`) мигрируют только вперёд: выпуск, созданный или открытый
новой версией, старый код может прочитать не так. После отката такие
выпуски не перезапускай; нужно вернуть данные как были — восстанови бэкап,
который `make deploy` сделал перед обновлением (runbook, сценарий 5).
