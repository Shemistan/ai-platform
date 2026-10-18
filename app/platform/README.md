# AI Platform

Локальный сервис на Mac (Apple Silicon): фото, видео, озвучка, статьи, новости,
каналы и сценарии «Дайджест новостей», «Видео по статье», «Пост в канал»; модели
локальные, озвучку и новости делает движок [`app/narrator`](../narrator/README.md).
Этот файл — для того, кто ставит и разрабатывает; как пользоваться вкладками и
сценариями — [гид пользователя](../../docs/user-guide.md).

- [Запуск](#запуск) · [Автозапуск](#автозапуск) · [Данные и бэкап](#данные-и-бэкап) · [Обновление и откат](#обновление-и-откат)
- [Переезд со старого narrator](#переезд-со-старого-narrator) · [Если не работает при установке](#если-не-работает-при-установке)
- [Настройка](#настройка) · [Установка моделей](#установка-моделей) · [Каналы публикации](#каналы-публикации)
- [Проверка](#проверка) · [Архитектура и API](#архитектура-и-api)

## Запуск

Что-то сломалось на живом сервисе — сразу в [runbook](../../docs/runbook.md).

```sh
cd app/platform
make demo   # без моделей и сети: фейковые генераторы, TTS, LLM и новости
make run    # на настоящих моделях
```

Открыть: http://127.0.0.1:8780. Браузеры: поддерживаются последние версии Chrome, Safari и
Firefox (страницы — ES-модули без сборки; фото с EXIF-поворотом открываются и в Safari до 17).
Нужны Python 3.11+ и ffmpeg с drawtext: `brew install ffmpeg-full` и её `bin` в PATH
(или `AIP_FFMPEG=$(brew --prefix ffmpeg-full)/bin/ffmpeg`) — формула `ffmpeg` собрана без freetype.
Системный `python3` на Mac — 3.9 из Xcode CLT: Makefile сам возьмёт `python3.13`,
`python3.12` или `python3.11` из PATH, а если их нет — остановится с подсказкой
(`brew install python@3.12` или `uv python install 3.12`, затем `make PYTHON=python3.12 …`).
Python-зависимостей у сервиса нет; у движка `app/narrator` они свои (uv), первый
`make check` в нём их ставит.

Живость — `/api/health`, готовность к дайджесту — `/api/ready` (503 и `failed`),
см. [`docs/api-routes.md`](../../docs/api-routes.md). Лог, остановка, лимиты времени —
[`docs/configuration.md`](../../docs/configuration.md#лог-остановка-лимиты-времени).

### Автозапуск

Нет процесса — нет дайджеста по расписанию. LaunchAgent поднимает сервис при входе
и после падения; ставить один раз из обычного терминала (агент запомнит каталог):

```sh
cd app/platform
make install-agent    # ~/Library/LaunchAgents/com.aiplatform.server.plist + чекер дайджеста
make agent-status     # состояние агентов и ответ /api/health
make uninstall-agent  # убрать оба агента; процесс больше не перезапускается
```

Внешний чекер (`tools/digest_check.py`): сервис молчит или дайджеста нет — пишет
ботом в Telegram мимо платформы. Токен бота и твой chat id — в Keychain:

```sh
security add-generic-password -U -s aiplatform-digest-check -a telegram-token -w '<токен бота>'
security add-generic-password -U -s aiplatform-digest-check -a telegram-chat -w '<chat id>'
make digest-check     # проверить вручную (или AIP_ALERT_TG_TOKEN / AIP_ALERT_TG_CHAT в env)
```

PATH под launchd, перезапуск, логи, время чекера, пробуждение Mac и heartbeat —
[`docs/configuration.md`](../../docs/configuration.md#автозапуск-подробности).

### Данные и бэкап

Данные — вне рабочей копии, в `~/Library/Application Support/aiplatform`
(`AIP_DATA_DIR`). Перенос (`make migrate-data`), шифрованный бэкап
(`make backup`, `make restore`), Time Machine (`make tm-exclude`) — [`docs/configuration.md`](../../docs/configuration.md#данные-и-бэкап); сроки хранения личных данных — [«Данные и сроки»](../../docs/configuration.md#данные-и-сроки).

### Обновление и откат

Живой сервис — отдельный checkout на теге релиза (`~/apps/aiplatform`,
`AIP_LIVE_DIR`): `make deploy TAG=v<версия>`, откат — тот же deploy с прошлым
тегом, `make deployed` — что сейчас. Первая настройка и ограничения отката —
[`docs/configuration.md`](../../docs/configuration.md#обновление-и-откат).

### Переезд со старого narrator

Один раз, из основного checkout: `sh app/narrator/tools/import-old.sh <old-narrator-dir>`
(каталог с `library/`, `telegram_tems.md`, `bot_token.md`). Скрипт их копирует и
ничего не удаляет; проверь «Статьи», «Новости», «Каналы» — и старый каталог можно удалить.

### Если не работает при установке

Грабли установки и разработки. Чего не хватает работающему сервису, он
пишет сам — баннер «Проблемы» вверху страниц и строки `⚠` при `make run`.

| Симптом | Причина | Что сделать |
|---|---|---|
| `SyntaxError` на `match` при `python3 -m aiplatform.server`, или `make run` останавливается с подсказкой про Python | системный `python3` на Mac — 3.9 из Xcode CLT, сервису нужен 3.11+ | `brew install python@3.12` (или `uv python install 3.12`), затем `make PYTHON=python3.12 run`; запускать через `make`, а не голым `python3` |
| Баннер «Аудио: у движка по умолчанию … нет весов» или озвучка падает с «нет модели …» | `make models` в `app/narrator` не запускали, или веса лежат на неподключённом диске | `cd app/narrator && make models`; для MLX-движков — `make models-mlx E=higgs`; другой каталог — `NARRATOR_MODELS_DIR=~/local_models` |
| Модели фото или видео нет в списке или задача падает без весов | `make models M=…` не запускали, `AIP_MODELS_DIR` указывает на неподключённый `/Volumes/External SSD` | подключить диск или `make models M=<id> AIP_MODELS_DIR=~/local_models`; что стоит — вкладка «Обновления» |
| FLUX.2 падает: `MLX error: Failed to load the default metallib` | рядом с бинарником Flux2CLI нет `mlx.metallib` (в релизном zip его нет; `setup-model.sh` докладывает его из колеса `mlx-metal==0.31.1`, но Flux2CLI переставили руками или докладка упала без сети к PyPI) | `make models M=flux2-klein-9b` (или `flux2-dev`) — повторный запуск доложит файл; без сети — `MLX_METALLIB=/путь/к/mlx.metallib make models M=flux2-klein-9b` |
| Первая генерация FLUX.2 Klein долго «висит» на старте, растёт `AIP_MODELS_DIR/models` | текстовый энкодер Klein (`lmstudio-community/Qwen3-8B-MLX-4bit`, ~5 ГБ) не поставлен: Klein ставили до того, как `setup-model.sh` научился его предзагружать | `make models M=flux2-klein-9b` — докачает энкодер в `AIP_MODELS_DIR/models/`; или подождать первый запуск (нужна сеть) |
| Дайджест или промпты слайдов падают: `claude: command not found` | claude CLI (Claude Code) не установлен или его нет в `PATH` процесса — у launchd `PATH` короче, чем в терминале | установить Claude Code, залогиниться, проверить `claude --version`; под launchd — полный путь в `AIP_LLM_CMD` и `CLAUDE=/полный/путь/claude` для `make news` движка |
| Агент (Claude Code) не может скачать модели, `make check` движка падает на `uv`, нет GPU | sandbox агента: сети к Hugging Face и PyPI нет, GPU нет, `~/.cache/uv` закрыт на запись | модели и первую установку зависимостей — в обычном терминале (`make models`, `make check` в `app/narrator`); `./narrate` сам переносит кэш uv в `$TMPDIR`, для своих команд — `UV_CACHE_DIR=$TMPDIR/uv-cache` |
| Шаг «Постер» дайджеста ждёт картинку, локальная модель обложку не рисует | так задумано: обложку дайджеста делают в ChatGPT по `image_prompt.md` выпуска — кириллические заголовки локальные модели рисуют хуже | сгенерировать в ChatGPT по промпту шага 2 и перетащить в шаг или положить `cover.*` в каталог выпуска narrator; локально — галочка «локальная модель» |
| `git commit` не гоняет `./check`, сообщение коммита не проверяется | хуки репозитория не включены на этой копии | `sh bootstrap.sh` в корне репозитория |

Эксплуатация живого сервиса — дайджест не вышел, GPU завис, диск не
примонтирован, Telegram не принимает, откат и восстановление из бэкапа, карта
конфигурации и ограничения — в [runbook](../../docs/runbook.md). Системные
ошибки агентов и предохранители — `docs/грабли.md`.

## Настройка

Переменные — в `local.mk` рядом с Makefile (пример: `cp local.mk.example local.mk`)
или в окружении; пусто — по умолчанию. Полный список и доступ по сети —
[`docs/configuration.md`](../../docs/configuration.md). Главное:

| Переменная | По умолчанию | Что это |
|---|---|---|
| `AIP_PORT`, `AIP_HOST` | `8780`, `127.0.0.1` | где слушать; не-loopback адрес — только с `AIP_AUTH_TOKEN` |
| `AIP_DATA_DIR` | `~/Library/Application Support/aiplatform` | задачи, загрузки, выпуски, настройки, каналы |
| `AIP_MODELS_DIR` | `/Volumes/External SSD/local_models` | веса фото и видео; пишет `make models` |
| `AIP_LLM_CMD` | `claude -p …` | LLM: промпт на stdin, ответ в stdout |
| `AIP_CHANNELS` | `<data>/channels.json` | каналы публикации |

Сервер по умолчанию слушает только `127.0.0.1` и проверяет Host и Origin;
`POST` из скриптов — с заголовком `X-AIP: 1`. Удалённо — SSH-туннель или
Tailscale, не открытый порт ([«Доступ и защита»](../../docs/configuration.md#доступ-и-защита)).

### Установка моделей

Фото и видео ставит `tools/setup-model.sh` из этого репозитория — соседние
репозитории photogen и videogen не нужны. Нужны Mac на Apple Silicon, сеть,
`uv` и ffmpeg (`brew install uv ffmpeg-full`), Python 3.11+ (см. «Запуск»). Из
`app/platform`:

```sh
make models M=--list                       # какие модели есть
make models M="z-image-turbo ltx-2.3-q4"   # минимум: одна модель фото и одна видео, без лицензий и токенов
make run
```

Скрипт ставит пайплайны в `~/.local/share/photogen` и `…/videogen`
(`PHOTOGEN_HOME`, `VIDEOGEN_HOME`), качает веса в `AIP_MODELS_DIR` и
дописывает в `local.mk` строки `AIP_MODELS_DIR`, `AIP_PHOTO_*`,
`AIP_VIDEO_*`, `AIP_AVATAR_*`, не трогая остальные. Повтор докачивает недостающее. Для
FLUX.2 сам докладывает `mlx.metallib` к Flux2CLI и ставит энкодер Klein.
Нет внешнего диска — `make models M=z-image-turbo AIP_MODELS_DIR=~/local_models`;
перенести скачанное на другой диск — `make move-models TO=~/local_models`.

Закрытые лицензией веса (FLUX.2, Ideogram 4, LTX-2.5): открыть страницу
модели на Hugging Face, принять условия, создать токен типа Read и запустить
`HF_TOKEN=hf_… make models M=<id>`; скрипт подскажет ссылку.

| Модель | Вкладка | Веса на диске | Примечание |
|---|---|---|---|
| `z-image-turbo` | фото | ~10 ГБ | по умолчанию для постеров и слайдов, без токена |
| `krea-2` | фото | ~15 ГБ | без токена |
| `qwen-image-2512` | фото | ~26 ГБ | без токена |
| `ideogram-4` | фото | требует замера (q4 mflux — обычно 10–25 ГБ) | нужен `HF_TOKEN` |
| `flux2-klein-9b` | фото | ~18 ГБ + ~5 ГБ текстовый энкодер Qwen3-8B (ставится вместе с моделью) | Flux2CLI, нужен `HF_TOKEN` |
| `flux2-dev` | фото | требует замера (оценка ~65 ГБ: Flux2CLI хранит bf16-веса 32B) | Flux2CLI, нужен `HF_TOKEN` |
| `ltx-2.3-q4` | видео | ~19 ГБ + 7,5 ГБ энкодер Gemma 3 12B | без токена; живое фото |
| `ltx-2.5-q4` | видео | ~27 ГБ (энкодер внутри) | модель видео по умолчанию, нужен `HF_TOKEN` |
| `wan-2.2-t2v-q4` | видео | ~27 ГБ | только по промпту, без звука |
| `sadtalker` | диктор | ~2,4 ГБ + окружение ~1 ГБ в `~/.local/share/avatar` | идёт на процессоре |

Размеры — `du` каталогов (у LTX — файлы режима `--distilled`, `FULL=1` — весь репозиторий);
сверху — пайплайны (~2 ГБ) и SeedVR2 (~7 ГБ, при первом увеличении). Обновил пайплайн —
`make contracts`: тест сверяет флаги платформы со справкой CLI в `tests/contracts/`.
Озвучка и новости: `make models` в `app/narrator`; дайджесту нужен claude CLI.

### Каналы публикации

Скопируй `channels.example.json` в `channels.json` и поправь. Типы:

- `telegram` — видео ботом (`sendVideo`, до 50 МБ) с подписью из шага
  «Публикация». Токен — в переменной из `token_env` (`AIP_TG_…`,
  `AIP_TELEGRAM_TOKEN` или `NARRATOR_TELEGRAM_TOKEN…`), в файлы и логи не пишется.
- `manual` — пакет `video.mp4`, `title.txt`, `description.txt` в
  `data/studio/projects/<id>/publish/<канал>/` для YouTube, TikTok, Instagram
  (почему не OAuth — [ADR 0008](../../docs/decisions/0008-manual-channels-not-oauth.md)).

`format`: `landscape` — видео 16:9, `vertical` — шортс 9:16. Без
`channels.json` доступны два пакета для ручной загрузки.

## Проверка

```sh
make check   # ruff (если стоит), node --check, тесты — всё на фейках
make test    # только тесты: параллельно, pytest-xdist (~1 мин); make test-serial — через unittest
AIP_TEST_TIMEOUT_SCALE=3 make test  # занятая машина: все ожидания в тестах ×3
make cov     # покрытие по модулям (худшие сверху) и html в tmp/cov/html, без порога
make smoke   # на настоящих моделях: картинка, фраза озвучки, черновик статьи — с временем шагов
```

Тесты гоняют вкладки и сценарии через HTTP с фейковыми моделями и движком
(`aiplatform/fakes/fake_narrate.py`) и настоящим ffmpeg; корневой `./check` — и
`app/narrator`. `make smoke` — на временном каталоге данных, `--fake` — на фейках.
Отчёты о прогонах на настоящих моделях — в [`docs/CHANGELOG.md`](../../docs/CHANGELOG.md), под версией прогона.

## Архитектура и API

Ключевые решения — ADR в [`docs/decisions/`](../../docs/decisions/README.md); прежде чем «исправлять»,
прочитай: stdlib HTTP без зависимостей (0003), один GPU-замок (0004), narrator
подпроцессом через `bridge` (0005), LLM через `claude -p` (0006), файлы `data/`
вместо БД (0007), ручные каналы вместо OAuth (0008). Маршруты HTTP API и команды
моста — [`docs/api-routes.md`](../../docs/api-routes.md) (собирается из кода, `./check` сверяет).
