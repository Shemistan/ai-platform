#!/bin/sh
# Покрытие кода тестами движка: `make cov`. Порога нет — отчёт по модулям в
# терминал (худшие сверху) и html в tmp/cov/html (tmp/ в .gitignore).
set -e
cd "$(dirname "$0")/.."
# В sandbox агента ~/.cache/uv закрыт на запись — кэш uv в $TMPDIR (как в check.sh).
if ! (mkdir -p "$HOME/.cache/uv" && : > "$HOME/.cache/uv/.cov-probe") 2>/dev/null; then
  export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
fi
rm -f "$HOME/.cache/uv/.cov-probe" 2>/dev/null
OUT=tmp/cov
mkdir -p "$OUT"
export COVERAGE_FILE="$OUT/.coverage"
# Экстры — как в check.sh: NARRATOR_CHECK_EXTRAS (по умолчанию все).
set -- uv run --quiet ${NARRATOR_CHECK_EXTRAS---all-extras} --with coverage coverage
"$@" run --source=src/narrator -m pytest -q
"$@" report --skip-empty --sort=cover
"$@" html --skip-empty -q -d "$OUT/html"
echo "html-отчёт: $(pwd)/$OUT/html/index.html"
