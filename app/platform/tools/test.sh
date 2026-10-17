#!/bin/sh
# Тесты платформы: `make test`. Параллельно — pytest-xdist по процессам (-n auto), каждый
# воркер — отдельный процесс со своим tmp (tests/__init__.py) и сервером на порту 0 (QA-016).
# pytest и xdist не входят в зависимости сервиса: берём установленные или через uv.
# Нет ни того ни другого (или uv без сети и кэша) — последовательный unittest, как `make test-serial`.
# Аргументы уходят в pytest: `sh tools/test.sh -k studio`, `-n 4`.
set -e
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
# ResourceWarning — ошибка: незакрытый файл или сокет в тесте — дефект (как в test-serial).
WARN="-W error::ResourceWarning"
OPTS="-q -p no:cacheprovider --dist worksteal"
[ -n "$AIP_TEST_WORKERS" ] && OPTS="$OPTS -n $AIP_TEST_WORKERS" || OPTS="$OPTS -n auto"

serial() {
  echo "⚠ $1 — тесты идут последовательно (медленно). Ускорить: brew install uv." >&2
  exec "$PY" $WARN -m unittest discover -s tests -t .
}

if "$PY" -c 'import pytest, xdist' 2>/dev/null; then
  set -- "$PY" -m pytest "$@"
elif command -v uv >/dev/null 2>&1; then
  # В sandbox агента ~/.cache/uv закрыт на запись — кэш uv в $TMPDIR (как в narrator и cov.sh).
  if ! (mkdir -p "$HOME/.cache/uv" && : > "$HOME/.cache/uv/.test-probe") 2>/dev/null; then
    export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
  fi
  rm -f "$HOME/.cache/uv/.test-probe" 2>/dev/null
  UV="uv run --quiet --no-project --python $PY --with pytest --with pytest-xdist"
  # Разрешение окружения проверяем отдельно: упавший тест не должен уводить в последовательный прогон.
  $UV python -c 'import pytest, xdist' >/dev/null 2>&1 || serial "uv не смог поставить pytest-xdist (нет сети?)"
  set -- $UV python -m pytest "$@"
else
  serial "нет pytest-xdist и uv"
fi
exec "$@" $WARN $OPTS tests
