.PHONY: run demo smoke test lint check install-agent uninstall-agent agent-status digest-check

PYTHON ?= python3

# local.mk — свои пути и команды (AIP_*), не в git. Пример — local.mk.example.
-include local.mk
# Переменные из local.mk должны дойти до сервиса: экспортируем все.
export

# Платформа на настоящих моделях: нужны установленные пайплайны photogen/videogen и narrator (см. README).
run:
	$(PYTHON) -m aiplatform.server

# Демо без моделей и сети: фейковые генераторы, TTS, LLM и новости — те же страницы и статусы.
demo:
	AIP_BACKEND=fake AIP_DATA_DIR=data-demo $(PYTHON) -m aiplatform.server

# Автозапуск через launchd (README «Автозапуск»): сервис при входе и после падения + внешний чекер дайджеста.
# Запускать из основного checkout, не из worktree: агент запомнит этот каталог.
install-agent:
	sh tools/agent.sh install

uninstall-agent:
	sh tools/agent.sh uninstall

agent-status:
	sh tools/agent.sh status

# Внешний чекер вручную: сервис жив, дайджест за день есть (иначе — сообщение в Telegram).
digest-check:
	$(PYTHON) tools/digest_check.py

# Короткий прогон на настоящих моделях с замером времени: картинка, фраза озвучки, черновик статьи.
smoke:
	$(PYTHON) -m aiplatform.smoke

test:
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests -t .

# ruff и node не обязательны для работы сервиса — проверяем, если установлены.
lint:
	@if command -v ruff >/dev/null 2>&1; then ruff check . && ruff format --check .; \
	  else echo "ruff не установлен — линт пропущен"; fi
	@if command -v node >/dev/null 2>&1; then for f in $$(find aiplatform/static -name '*.js'); do node --check $$f || exit 1; done; \
	  else echo "node не установлен — проверка JS пропущена"; fi

# Зовётся из корневого ./check (pre-commit, ./finish). Красный → коммит отменяется.
check: lint test
