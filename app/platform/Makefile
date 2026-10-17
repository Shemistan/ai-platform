.PHONY: run demo smoke test lint check python-check

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

# Платформа на настоящих моделях: нужны установленные пайплайны photogen/videogen и narrator (см. README).
run: python-check
	$(PYTHON) -m aiplatform.server

# Демо без моделей и сети: фейковые генераторы, TTS, LLM и новости — те же страницы и статусы.
demo: python-check
	AIP_BACKEND=fake AIP_DATA_DIR=data-demo $(PYTHON) -m aiplatform.server

# Короткий прогон на настоящих моделях с замером времени: картинка, фраза озвучки, черновик статьи.
smoke: python-check
	$(PYTHON) -m aiplatform.smoke

test: python-check
	$(PYTHON) -W error::ResourceWarning -m unittest discover -s tests -t .

# ruff и node не обязательны для работы сервиса — проверяем, если установлены.
lint:
	@if command -v ruff >/dev/null 2>&1; then ruff check . && ruff format --check .; \
	  else echo "ruff не установлен — линт пропущен"; fi
	@if command -v node >/dev/null 2>&1; then for f in $$(find aiplatform/static -name '*.js'); do node --check $$f || exit 1; done; \
	  else echo "node не установлен — проверка JS пропущена"; fi

# Зовётся из корневого ./check (pre-commit, ./finish). Красный → коммит отменяется.
check: python-check lint test

# Понятная ошибка вместо SyntaxError на старом Python.
python-check:
	@$(PYTHON) -c '$(PY_OK)' 2>/dev/null || { \
	  v=$$($(PYTHON) -c 'import platform; print(platform.python_version())' 2>/dev/null || echo "—"); \
	  w=$$(command -v $(PYTHON) 2>/dev/null || echo "$(PYTHON) не найден в PATH"); \
	  echo "⛔ Нужен Python 3.11+, найден $$v ($$w)." >&2; \
	  echo "   Поставь: brew install python@3.12 или uv python install 3.12," >&2; \
	  echo "   затем: make PYTHON=python3.12 $(or $(MAKECMDGOALS),run)" >&2; \
	  exit 1; }
