#!/bin/sh
# Сдвинуть staging fast-forward на HEAD (или на указанный коммит) ОДНОЙ командой:
#   tools/advance-staging.sh [<коммит>]
# Проверка «staging — предок цели» и update-ref идут в одном процессе, а
# update-ref сверяет старое значение (compare-and-swap): если staging сдвинул
# другой агент между проверкой и записью — отказ, а не потеря чужих коммитов.
# Не заменять на `git branch -f` / `reset` / update-ref без старого значения —
# см. docs/грабли.md, запись 3.
set -e
fail() { echo "⛔ advance-staging: $1" >&2; exit 1; }

TARGET=$(git rev-parse --verify --quiet "${1:-HEAD}^{commit}") || \
  fail "нет коммита ${1:-HEAD}"
OLD=$(git rev-parse --verify --quiet "refs/heads/staging^{commit}") || \
  fail "ветки staging нет — создай её от main: git branch staging main"

if [ "$OLD" = "$TARGET" ]; then
  echo "✓ staging уже на $(git rev-parse --short "$TARGET")"
  exit 0
fi
git merge-base --is-ancestor "$OLD" "$TARGET" || \
  fail "staging ($(git rev-parse --short "$OLD")) не предок цели — сначала git merge staging"
git update-ref -m "advance-staging: fast-forward" refs/heads/staging "$TARGET" "$OLD" || \
  fail "staging сдвинули параллельно — git merge staging и повтори"
echo "✓ staging $(git rev-parse --short "$OLD") → $(git rev-parse --short "$TARGET")"
