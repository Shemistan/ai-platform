#!/bin/sh
# Один раз: данные старого narrator → сюда. Копирует, ничего не удаляет.
#   sh tools/import-old.sh <old-narrator-dir>
# Что берёт: library/ (статьи, база новостей, подписчики бота),
# telegram_tems.md (каналы), bot_token.md (токен бота).
# Что уже есть здесь — не перезаписывает (rsync --ignore-existing).
# Запускать в обычном терминале из основного checkout: sandbox агента
# не пишет в library/, а worktree держит библиотеку в основном checkout.
set -e
OLD=${1:?укажи каталог старого narrator: sh tools/import-old.sh <old-narrator-dir>}
HERE=$(cd "$(dirname "$0")/.." && pwd)
[ -d "$OLD/library" ] || { echo "в $OLD нет library/ — это не каталог narrator" >&2; exit 2; }

echo "библиотека: $OLD/library → $HERE/library"
mkdir -p "$HERE/library"
rsync -a --ignore-existing --exclude '.DS_Store' "$OLD/library/" "$HERE/library/"

for f in telegram_tems.md bot_token.md; do
  if [ -f "$OLD/$f" ] && [ ! -f "$HERE/$f" ]; then
    cp -p "$OLD/$f" "$HERE/$f"
    echo "$f: скопирован"
  elif [ -f "$HERE/$f" ]; then
    echo "$f: уже есть — не трогаю"
  fi
done
chmod 600 "$HERE/bot_token.md" 2>/dev/null || true

echo
echo "Готово. Проверь: ./narrate news where --date yesterday, ./narrate bot channels."
echo "Старый каталог больше не нужен платформе — удалить его можно, когда убедишься,"
echo "что всё на месте (du -sh $HERE/library против du -sh $OLD/library)."
