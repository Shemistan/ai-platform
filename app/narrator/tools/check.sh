#!/bin/sh
# Линтер и тесты движка: `make check` (его зовёт корневой ./check).
set -e
cd "$(dirname "$0")/.."
# В sandbox агента ~/.cache/uv закрыт на запись — кэш uv в $TMPDIR.
if ! (mkdir -p "$HOME/.cache/uv" && : > "$HOME/.cache/uv/.check-probe") 2>/dev/null; then
  export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
fi
rm -f "$HOME/.cache/uv/.check-probe" 2>/dev/null
uv run --quiet --all-extras ruff check src tests
uv run --quiet --all-extras ruff format --check src tests
uv run --quiet --all-extras pytest -q
