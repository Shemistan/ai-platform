#!/bin/sh
# Включить гейты репозитория на этой машине (запускать из корня проекта).
cd "$(dirname "$0")" || exit 1
git config core.hooksPath .githooks
chmod +x .githooks/pre-commit .githooks/commit-msg .githooks/pre-push \
  check bootstrap.sh 2>/dev/null
for f in ai finish install-global.sh uninstall-global.sh backup-global.sh \
  new-project.sh .claude/hooks/preflight.sh; do
  [ -f "$f" ] && chmod +x "$f"
done
echo "✓ Гейты включены: core.hooksPath=.githooks"
