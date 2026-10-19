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
brew install uv ffmpeg-full ruff python@3.12   # системный python3 на Mac — 3.9, нужен 3.11+
export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"  # ffmpeg-full keg-only; формула ffmpeg — без drawtext
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
- Все переменные окружения — [`docs/configuration.md`](docs/configuration.md).
- Как пользоваться вкладками и сценариями — [гид пользователя](docs/user-guide.md);
  HTTP API — [`docs/api-routes.md`](docs/api-routes.md).
- Не запускается — [«Если не работает при установке»](app/platform/README.md#если-не-работает-при-установке).

## Проверки

Все проверки — локально, CI нет (DX-017: лимит GitHub Actions исчерпан,
workflow убран). `./check` гоняют три точки, обойти ни одну нельзя:

- `pre-commit` — на каждый коммит (ставит `sh bootstrap.sh`);
- `./finish` — перед push ветки и сдвигом `staging`;
- `pre-push` — перед любым push.

`./check` — гейты документации и бэклога, `make check` в каждом сервисе
`app/<svc>/`. Покрытие без порога — `make cov` в `app/platform` и
`app/narrator` (html — в их `tmp/cov/html`). PR `staging → main` сливается
после зелёного `./finish` на машине автора; статусов на GitHub у PR нет — если
в защите `main` остался обязательный статус `check`, его надо снять:
Settings → Branches → правило для `main` → Require status checks.

Вернуть CI: workflow лежит в истории (`git log -- .github/workflows/check.yml`),
раннер `macos-14`, Python 3.12, uv, `NARRATOR_CHECK_EXTRAS=`.

## Регламент агента

Правила для Claude Code — `CLAUDE.md`; механизм, который их держит, ставится
на машину отдельно (решение — [ADR 0002](docs/decisions/0002-agent-config-templates.md)):

1. `sh bootstrap.sh` в главном checkout: git-хуки (`main` агенту не
   коммитится и никому не пушится) и `.claude/settings.json` с
   `.claude/hooks/preflight.sh` из шаблонов `tools/claude/` — если их ещё
   нет. Свои правки `.claude/` не перетираются; отличие от шаблона
   `bootstrap.sh` показывает командой `diff`.
2. Общие хуки агента в `~/.claude/hooks/` — `preflight.sh` (worktree на
   старте задачи) и `guard.py` (PreToolUse: запрет правок `main`,
   `--no-verify`, подмены хуков). Их ставит `install-global.sh` из скелета
   ai-god-mode. Без них preflight блокирует промпты с подсказкой, а
   `bootstrap.sh` предупреждает.
3. Проверка: `claude` в корне — первый промпт проходит preflight; попытка
   агента закоммитить в `main` отклоняется `pre-commit`.

Поменять права агента для всех — правка шаблона в `tools/claude/` через PR;
на своей машине — `diff` и копия руками.
