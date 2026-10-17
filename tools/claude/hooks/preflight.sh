#!/bin/sh
# UserPromptSubmit: обёртка над общим ~/.claude/hooks/preflight.sh (его ставит
# install-global.sh из скелета). Логику правь в global/hooks/preflight.sh скелета,
# а не здесь — тогда фикс разойдётся по всем проектам одним install-global.sh.
# exit 2 = промпт блокируется, stderr уходит пользователю и модели.

G="$HOME/.claude/hooks/preflight.sh"
# Вместо общего хука лежит обёртка (проект в $HOME) — не зацикливаемся.
if [ -n "$AI_PREFLIGHT_WRAPPED" ] || [ ! -f "$G" ]; then
  echo "⛔ общий preflight не установлен — запусти install-global.sh из скелета ai-god-mode" >&2
  exit 2
fi
AI_PREFLIGHT_WRAPPED=1 exec sh "$G"
