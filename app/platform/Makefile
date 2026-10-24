.PHONY: run demo smoke contracts asr-compare test test-serial cov lint check release-prep release-tag python-check models move-models install-agent \
	uninstall-agent agent-status digest-check backup restore migrate-data tm-exclude deploy deployed

# Нужен Python 3.11+. Не задан (make PYTHON=… или окружение) — берём первый подходящий
# из PATH: на чистом Mac python3 — это 3.9 из Xcode CLT, а python3.12 из brew лежит рядом.
PY_OK = import sys; sys.exit(sys.version_info < (3, 11))
ifeq ($(origin PYTHON),undefined)
PYTHON := $(shell for p in python3 python3.13 python3.12 python3.11; do \
  $$p -c '$(PY_OK)' 2>/dev/null && { echo $$p; exit 0; }; done; echo python3)
endif

# local.mk — свои пути и команды (AIP_*), не в git. Пример — local.mk.example.
-include local.mk
# Переменные из local.mk должны дойти до сервиса: экспортируем все.
export

# Платформа на настоящих моделях: нужны модели (make models) и narrator (см. README).
run: python-check
	$(PYTHON) -m aiplatform.server

# Демо без моделей и сети: фейковые генераторы, TTS, LLM и новости — те же страницы и статусы.
demo: python-check
	AIP_BACKEND=fake AIP_DATA_DIR=data-demo $(PYTHON) -m aiplatform.server

# Автозапуск через launchd (README «Автозапуск»): сервис при входе и после падения + внешний чекер дайджеста.
# Запускать из основного checkout, не из worktree: агент запомнит этот каталог.
install-agent: python-check
	sh tools/agent.sh install

uninstall-agent:
	sh tools/agent.sh uninstall

agent-status:
	sh tools/agent.sh status

# Внешний чекер вручную: сервис жив, дайджест за день есть (иначе — сообщение в Telegram).
digest-check: python-check
	$(PYTHON) tools/digest_check.py

# Бэкап невосстановимых данных (README «Бэкап»): зашифрованный age архив в AIP_BACKUP_DIR, ротация 30 дней.
backup: python-check
	$(PYTHON) -m aiplatform.backup create

# Восстановление в пустой каталог: make restore FILE=<архив> TO=<каталог>.
restore: python-check
	@test -n "$(FILE)" -a -n "$(TO)" || { echo "укажи архив и пустой каталог: make restore FILE=… TO=…" >&2; exit 2; }
	$(PYTHON) -m aiplatform.backup restore "$(FILE)" "$(TO)"

# Один раз: data/ из рабочей копии → ~/Library/Application Support/aiplatform (копия; старое не удаляется).
# С TO=<пустой каталог> — текущие данные туда, например на внешний SSD; затем AIP_DATA_DIR в local.mk.
migrate-data: python-check
	$(PYTHON) -m aiplatform.backup migrate $(if $(TO),"$(TO)")

# Исключить из Time Machine восстановимое и тяжёлое: веса моделей, кэш генераций, .venv движка, кэш HF.
tm-exclude:
	@for p in "$${AIP_MODELS_DIR:-/Volumes/External SSD/local_models}" \
	  "$${AIP_DATA_DIR:-$$HOME/Library/Application Support/aiplatform}/studio/cache" \
	  "$(CURDIR)/../narrator/.venv" "$$HOME/.cache/huggingface"; do \
	  if [ -e "$$p" ]; then tmutil addexclusion "$$p" && echo "исключено из Time Machine: $$p"; \
	  else echo "нет, пропущено: $$p"; fi; done

# Живой checkout (AIP_LIVE_DIR, ~/apps/aiplatform) на тег релиза (make release-tag, DX-009):
# бэкап → checkout тега → перезапуск агента. Откат — то же с прошлым тегом (README «Обновление и откат»).
deploy:
	sh tools/deploy.sh deploy "$(TAG)"

# Какой тег у живого сервиса.
deployed:
	@sh tools/deploy.sh current

# Короткий прогон на настоящих моделях с замером времени: картинка, фраза озвучки, черновик статьи.
smoke: python-check
	$(PYTHON) -m aiplatform.smoke

# Снапшоты --help настоящих CLI генерации в tests/contracts/ (QA-005): после обновления пайплайна.
# Тест сверяет с ними флаги build_argv. Один снапшот: make contracts C=flux2-t2i.
contracts: python-check
	$(PYTHON) -m aiplatform.contracts $(C)

# Сравнение моделей расшифровки монтажа на своей записи с эталонным текстом (PRD-020):
# make asr-compare REC=запись.mp4 REF=teleprompter.md [M="whisper-large-v3-turbo qwen3-asr-1.7b"] [TERMS="Claude Code,ffmpeg"].
asr-compare: python-check
	@test -n "$(REC)" -a -n "$(REF)" || { echo "укажи запись и эталон: make asr-compare REC=… REF=…" >&2; exit 2; }
	$(PYTHON) -m aiplatform.montage.compare "$(REC)" "$(REF)" $(if $(M),--models $(M),) $(if $(TERMS),--terms "$(TERMS)",)

# Пайплайны и веса фото и видео (нужны сеть и Mac на Apple Silicon): make models M="z-image-turbo ltx-2.3-q4".
# Другой каталог весов: … AIP_MODELS_DIR=~/local_models. Список моделей: make models M=--list.
models: python-check
	@test -n "$(M)" || { echo "укажи модели: make models M=\"z-image-turbo ltx-2.3-q4\" (список: make models M=--list)" >&2; exit 2; }
	sh tools/setup-model.sh $(M)

# Перенос всех весов на другой диск с правкой AIP_MODELS_DIR в local.mk: make move-models TO=~/local_models.
move-models: python-check
	@test -n "$(TO)" || { echo "куда переносить: make move-models TO=~/local_models" >&2; exit 2; }
	sh tools/setup-model.sh --move-to "$(TO)"

# Параллельно (pytest-xdist по процессам, QA-016): pytest берётся свой или через uv, без них —
# последовательно. Аргументы pytest — через T: make test T="-k studio -n 4".
test: python-check
	PYTHON=$(PYTHON) sh tools/test.sh $(T)

# Последовательно и без pytest — для отладки: порядок и вывод как раньше.
test-serial: python-check
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests -t .

# Покрытие тестами без порога: отчёт по модулям и html в tmp/cov/html (coverage — свой или через uv).
cov: python-check
	PYTHON=$(PYTHON) sh tools/cov.sh

# Релиз (docs/README.md «Версии»): версия = дата релиза, тег v<версия>.
# release-prep — в ветке задачи: версия во всех файлах и заголовок в docs/CHANGELOG.md, дальше PR.
# release-tag — человек в main после слияния PR: тег на HEAD, если версия совпадает.
release-prep: python-check
	$(PYTHON) ../../tools/release.py prep $(if $(DATE),--date $(DATE),)

release-tag: python-check
	$(PYTHON) ../../tools/release.py tag

# ruff и node не обязательны для работы сервиса — проверяем, если установлены.
# node: синтаксис static/*.js и юнит-тесты фронтенда tests/js/*.test.mjs (node --test).
lint:
	@if command -v ruff >/dev/null 2>&1; then ruff check . && ruff format --check .; \
	  else echo "ruff не установлен — линт пропущен"; fi
	@if command -v node >/dev/null 2>&1; then for f in $$(find aiplatform/static -name '*.js'); do node --check $$f || exit 1; done; \
	  node --test --test-timeout=20000 tests/js/ >/dev/null || node --test --test-timeout=20000 tests/js/; \
	  else echo "node не установлен — проверка JS пропущена"; fi

# Зовётся из корневого ./check (./finish, ручной прогон). Красный → push отменяется.
check: python-check lint test

# Зовётся из корневого ./check --fast (pre-commit): линт и JS без pytest, секунды (DX-018).
check-fast: python-check lint

# Понятная ошибка вместо SyntaxError на старом Python.
python-check:
	@$(PYTHON) -c '$(PY_OK)' 2>/dev/null || { \
	  v=$$($(PYTHON) -c 'import platform; print(platform.python_version())' 2>/dev/null || echo "—"); \
	  w=$$(command -v $(PYTHON) 2>/dev/null || echo "$(PYTHON) не найден в PATH"); \
	  echo "⛔ Нужен Python 3.11+, найден $$v ($$w)." >&2; \
	  echo "   Поставь: brew install python@3.12 или uv python install 3.12," >&2; \
	  echo "   затем: make PYTHON=python3.12 $(or $(MAKECMDGOALS),run)" >&2; \
	  exit 1; }
