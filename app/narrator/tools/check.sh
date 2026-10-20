#!/bin/sh
# Линтер и тесты движка: `make check` (его зовёт корневой ./check).
# С --fast — только линтер: `make check-fast` из `./check --fast` (pre-commit, DX-018).
set -e
cd "$(dirname "$0")/.."
# В sandbox агента ~/.cache/uv закрыт на запись — кэш uv в $TMPDIR.
if ! (mkdir -p "$HOME/.cache/uv" && : > "$HOME/.cache/uv/.check-probe") 2>/dev/null; then
  export UV_CACHE_DIR="${TMPDIR:-/tmp}/uv-cache"
fi
rm -f "$HOME/.cache/uv/.check-probe" 2>/dev/null
# Экстры с моделями (espeech тянет torch): по умолчанию все, как на машине автора.
# CI ставит NARRATOR_CHECK_EXTRAS= (пусто) — тесты их не импортируют.
X=${NARRATOR_CHECK_EXTRAS---all-extras}
uv run --quiet $X ruff check src tests
uv run --quiet $X ruff format --check src tests
[ "$1" = "--fast" ] || uv run --quiet $X pytest -q
