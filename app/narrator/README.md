# narrator — движок озвучки и новостей платформы

Всё, что видит человек, — страницы платформы (`app/platform`, `make run`):
«Аудио», «Статьи», «Новости», «Каналы», сценарии дайджеста и поста. Этот
каталог — то, что они вызывают: синтез речи на локальных моделях, перевод
текста в произношение, проверка распознаванием, библиотека статей, сбор
новостей с агентом и судьёй, Telegram-бот. Каждую команду можно запустить
и из терминала.

## Первый запуск

В обычном терминале, не внутри Claude Code: sandbox агента не даёт ни сети
к моделям, ни видеокарты.

```sh
cd app/narrator
make models        # модели ~3,7 ГБ в NARRATOR_MODELS_DIR (у автора — /Volumes/External SSD/local_models)
make check         # первый запуск ставит Python-зависимости (torch, f5-tts) — несколько минут
make skills        # skill youtube-news для обзоров недели и месяца
```

Нужны `uv`, `ffmpeg`, `curl`; для дайджеста и обзоров — `claude` в PATH.

### Перенос со старого narrator

Данные — личные и в git не идут: `library/` (статьи, база новостей,
подписчики бота), `telegram_tems.md` (каналы), `bot_token.md` (токен бота).
Со старого каталога их забирает один скрипт, он только копирует:

```sh
sh tools/import-old.sh <old-narrator-dir>   # каталог старого narrator с library/
```

Запускай из основного checkout: библиотека одна на машину, worktree её не
держат. После проверки старый каталог можно удалить.

## Команды

`make` без аргументов — список по группам, `./narrate <команда> -h` — флаги.
День — `DAY=2026-09-22`, `today` или `yesterday` (по умолчанию вчера).

| Команда | Что делает |
|---|---|
| **Озвучка** | |
| `make say F=файл.md ENGINE=silero` | озвучить любой файл как есть: mp3 и отчёт в `tmp/say/` |
| `make render` | озвучить и проверить все статьи библиотеки, где текст новее аудио |
| `make render-all` | переозвучить все статьи |
| `make synth A=2026-09-18/triple-debt` | одна статья; `make verify A=…` — проверить её |
| `./narrate normalize файл.md` | как текст прочитает модель |
| **Библиотека** | |
| `./narrate fetch <url>` | скачать статью в `library/` |
| `make list` | реестр `library/index.md` |
| **Новости** | |
| `make news DAY=…` | сбор → агент пишет выпуск → судья проверяет (`review.md`) |
| `make news-collect`, `news-agent`, `news-judge` | шаги по отдельности |
| `make news-rescue DAY=…` | агент добирает материалы упавших источников |
| `make news-top`, `make news-sources` | главное за день; сбои и польза источников |
| `make news-weekly`, `make news-monthly MONTH=ГГГГ-ММ` | обзоры для YouTube: `library/youtube_news/…/teleprompter.md` |
| `make news-publish DAY=…` | выпуск в канал из терминала (`NEWS_POST=photo` — двумя постами) |
| **Telegram** | |
| `make channels` | каналы из `telegram_tems.md` |
| `make unsent`, `make send-unsent`, `make mark-sent` | неотправленные озвучки статей |
| `make subscribers`, `make bot` | подписчики бота; приём `/start` и `/stop` |
| `./narrate bot forget <chat_id>` | удалить подписчика по его просьбе |
| **Модели и голоса** | |
| `make models-mlx E="qwen3 chatterbox"` | модели для сравнения на mlx-audio (~45 ГБ все) |
| `make move-models` | перенести модели из `~/.cache` в папку моделей |
| `make voice-charon` | образец голоса Charon из Gemini TTS (`GEMINI_API_KEY`) |
| **Для платформы** | |
| `./narrate bridge <команда>` | данные страницам — JSON (`src/narrator/bridge.py`) |

Движки: `espeech` (лучшее качество, видеокарта), `silero` (секунды на CPU,
CC BY-NC), `supertonic` (быстро на CPU), для сравнения на Apple Silicon —
`higgs`, `fish`, `moss`, `qwen3`, `chatterbox`. Голоса-клоны — `Charon`,
`M1`, `Shem`, `Shem-v2`.

## Как устроено

- `src/narrator/` — пакет: `synth.py` (движки), `normalize.py` и
  `lexicon.py` (произношение), `stress.py` (ударения), `verify.py`
  (распознавание), `library.py` (статьи), `telegram.py` (бот),
  `news/` (источники, база, отбор, обложки), `bridge.py` (JSON для
  платформы).
- `config/news-sources.toml` — источники новостей.
- `CLAUDE.md` — регламент агентов: подготовка статьи, дайджест, судья,
  добор, обзоры.
- `docs/tts-guide.md` — правила текста для озвучки, `docs/news/` — подборки
  источников.

## Подписчики бота и личные данные

О подписчике в `library/telegram.json` хранится только chat_id, дата
подписки и тема (если это канал-тема). Имя не пишется: `bot subscribers`
показывает chat_id маской `chat …1234`, полный id и имя из Telegram — с
`--names`, на лету. `/stop` и блокировка бота удаляют запись; по просьбе —
`./narrate bot forget <chat_id>`. Старый файл с именами и отписавшимися
чистится при первом сохранении.

Бот при `/start` пишет, что хранит, и даёт ссылку на
[`docs/privacy.md`](docs/privacy.md) (другой адрес —
`$NARRATOR_PRIVACY_URL`). Перед ростом аудитории сверить с юристом
формулировки политики и нужно ли уведомление РКН об обработке ПДн
(152-ФЗ ст. 22).
