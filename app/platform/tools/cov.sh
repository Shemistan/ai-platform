#!/bin/sh
# Покрытие кода тестами: `make cov`. Порога нет — отчёт по модулям в терминал
# (худшие сверху) и html в tmp/cov/html (tmp/ в .gitignore).
# coverage не входит в зависимости сервиса: берём установленный или через uv.
set -e
cd "$(dirname "$0")/.."
PY="${PYTHON:-python3}"
OUT=tmp/cov
mkdir -p "$OUT"
export COVERAGE_FILE="$OUT/.coverage"
if "$PY" -c 'import coverage' 2>/dev/null; then
  set -- "$PY" -m coverage
elif command -v uv >/dev/null 2>&1; then
  # В sandbox агента ~/.cache/uv закрыт на запись — кэш uv в $TMPDIR (как в narrator).
  if ! (mkdir -p "$HOME/.cache/uv" && : > "$HOME/.cache/uv/.cov-probe") 2>/dev/null; then
    export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
  fi
  rm -f "$HOME/.cache/uv/.cov-probe" 2>/dev/null
  set -- uv run --quiet --no-project --python "$PY" --with coverage python -m coverage
else
  echo "⛔ Нужен coverage: brew install uv (возьмём через uv) или $PY -m pip install coverage." >&2
  exit 1
fi
"$@" run --source=aiplatform -m unittest discover -s tests -t .
"$@" report --skip-empty --sort=cover
"$@" html --skip-empty -q -d "$OUT/html"
echo "html-отчёт: $(pwd)/$OUT/html/index.html"
