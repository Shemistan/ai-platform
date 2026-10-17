#!/bin/sh
# Включить гейты репозитория на этой машине (запускать из корня проекта).
# 1. git-хуки: core.hooksPath=.githooks (pre-commit, commit-msg, pre-push).
# 2. Регламент агента: .claude/settings.json и .claude/hooks/ из шаблонов
#    tools/claude/ — только если их ещё нет; свои правки не перетираем
#    (ADR 0002: права агента включает человек, а не git pull).
cd "$(dirname "$0")" || exit 1
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push \
  check bootstrap.sh 2>/dev/null
[ -f finish ] && chmod +x finish
echo "✓ Гейты включены: core.hooksPath=.githooks"

# Агентный регламент — только в главном checkout: worktree берут его оттуда.
if [ -f .git ]; then
  echo "· worktree: .claude/ ставится в главном checkout"
  exit 0
fi
for rel in settings.json hooks/preflight.sh; do
  src="tools/claude/$rel"
  dst=".claude/$rel"
  [ -f "$src" ] || continue
  if [ ! -f "$dst" ]; then
    mkdir -p "$(dirname "$dst")"
    cp "$src" "$dst"
    echo "✓ $dst — из шаблона $src"
  elif ! cmp -s "$src" "$dst"; then
    echo "· $dst отличается от шаблона — сравни: diff $src $dst"
  fi
done
[ -f .claude/hooks/preflight.sh ] && chmod +x .claude/hooks/preflight.sh
if [ ! -f "$HOME/.claude/hooks/preflight.sh" ] || [ ! -f "$HOME/.claude/hooks/guard.py" ]; then
  echo "⚠ нет общих хуков агента (~/.claude/hooks/preflight.sh, guard.py): без них preflight"
  echo "  блокирует промпты, а guard не стоит. Поставь их — README, «Регламент агента»."
fi
exit 0
