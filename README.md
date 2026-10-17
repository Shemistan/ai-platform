# ai-platform

Локальная AI-платформа: вкладки «Фото», «Видео», «Аудио», «Выпуски», «Статьи»,
«Новости», «Каналы» и сценарии «Дайджест новостей», «Видео по статье» и
«Пост в канал» в одном сервисе.

Сервис — `app/platform/`, запуск и настройка — в [его README](app/platform/README.md).
Движок озвучки, статей, новостей и Telegram-бота, который он вызывает, —
`app/narrator/` ([README](app/narrator/README.md)).

```sh
cd app/platform && make demo   # http://127.0.0.1:8780
```

Дизайн — `docs/superpowers/specs/2026-10-15-ai-platform-design.md`.

## Быстрый старт на чистом Mac

Нужен Mac на Apple Silicon. Три уровня: каждый следующий опирается на
предыдущий, остановиться можно на любом.

### 1. Демо — без моделей и сети

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"  # Homebrew, если его нет
brew install uv ffmpeg ruff python@3.12   # системный python3 на Mac — 3.9, нужен 3.11+
git clone <url репозитория> ai-platform && cd ai-platform
sh bootstrap.sh                           # включить git-хуки репозитория
cd app/platform && make demo              # http://127.0.0.1:8780 — фейковые модели
```

`bootstrap.sh` один раз на машину ставит `core.hooksPath=.githooks`. Без
него хуки молчат: `pre-commit` не гоняет `./check`, `commit-msg` не
проверяет формат сообщения, `pre-push` не защищает `main`. Сервису хуки не
нужны — они нужны тому, кто коммитит.

`ruff` не обязателен: без него `make check` пропускает линт.

### 2. Озвучка — настоящие голоса

```sh
cd app/narrator && make models            # ~3,7 ГБ: ESpeech, Silero, Supertonic, RUAccent
```

Модели ложатся в `/Volumes/External SSD/local_models`; без внешнего диска —
`export NARRATOR_MODELS_DIR=~/local_models` до `make models`. После этого
озвучка по умолчанию — ESpeech. Higgs (движок дайджеста, клон голоса на
MLX) ставится отдельно: `make models-mlx E=higgs`; когда его веса на месте,
платформа берёт его сама. Чего не хватает — пишет баннер «Проблемы» вверху
страниц с командой установки.

### 3. Всё — фото, видео, дайджест

```sh
cd app/platform
make models M="z-image-turbo ltx-2.3-q4"  # минимум: одна модель фото и одна видео
make run                                  # http://127.0.0.1:8780 — настоящие модели
```

- Остальные модели фото и видео, закрытые лицензией веса и `HF_TOKEN` —
  [«Установка моделей»](app/platform/README.md#установка-моделей).
- Дайджест новостей и тексты слайдов пишет claude CLI (Claude Code): он
  должен быть в `PATH` и залогинен — `claude --version`.
- Публикация в Telegram — [«Каналы публикации»](app/platform/README.md#каналы-публикации).
- Все переменные окружения — [«Настройка»](app/platform/README.md#настройка).
- Не запускается — [«Если не работает при установке»](app/platform/README.md#если-не-работает-при-установке).

## Проверки и CI

Локально — `./check` (его же зовёт `pre-commit`): гейты документации и
бэклога, `make check` в каждом сервисе `app/<svc>/`. Покрытие без порога —
`make cov` в `app/platform` и `app/narrator` (html — в их `tmp/cov/html`).

Независимо от машины тот же `./check` гоняет GitHub Actions
(`.github/workflows/check.yml`): на каждый PR в `main` и push в `staging`,
раннер `macos-14` с ffmpeg, Python 3.12 и uv, затем покрытие в лог джобы.
Экстры narrator с моделями (torch) в CI не ставятся — `NARRATOR_CHECK_EXTRAS=`.

Сделать проверку обязательной — один раз, вручную, в настройках репозитория
на GitHub: Settings → Branches (или Rules → Rulesets) → правило для `main` →
Require status checks to pass → добавить `check` (появится в списке после
первого прогона workflow). Тогда PR `staging → main` не сольётся с красным
статусом.
