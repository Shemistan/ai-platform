.PHONY: run demo test lint check

PYTHON ?= python3

# local.mk — свои пути и команды (AIP_*), не в git. Пример — README.md, раздел «Настройка».
-include local.mk

# Платформа на настоящих моделях: нужны установленные пайплайны photogen/videogen и narrator (см. README).
run:
	$(PYTHON) -m aiplatform.server

# Демо без моделей и сети: фейковые генераторы, TTS, LLM и новости — те же страницы и статусы.
demo:
	AIP_BACKEND=fake AIP_DATA_DIR=data-demo $(PYTHON) -m aiplatform.server

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
