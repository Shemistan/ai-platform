#!/bin/sh
# Один раз: данные старого narrator → сюда. Копирует, ничего не удаляет.
#   sh tools/import-old.sh <old-narrator-dir>
# Что берёт: library/ (статьи, база новостей, подписчики бота),
# telegram_tems.md (каналы), bot_token.md (токен бота — строкой
# NARRATOR_TELEGRAM_TOKEN=… в .env в корне репозитория).
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

f=telegram_tems.md
if [ -f "$OLD/$f" ] && [ ! -f "$HERE/$f" ]; then
  cp -p "$OLD/$f" "$HERE/$f"
  echo "$f: скопирован"
elif [ -f "$HERE/$f" ]; then
  echo "$f: уже есть — не трогаю"
fi

ENV="$HERE/../../.env"
if [ -f "$OLD/bot_token.md" ]; then
  if grep -qs '^ *\(export \)\{0,1\}NARRATOR_TELEGRAM_TOKEN *=' "$ENV"; then
    echo "токен: в .env уже есть — не трогаю"
  elif token=$(grep -oE '[0-9]{6,}:[A-Za-z0-9_-]{30,}' "$OLD/bot_token.md" | head -n 1) && [ -n "$token" ]; then
    (umask 077; printf 'NARRATOR_TELEGRAM_TOKEN=%s\n' "$token" >> "$ENV")
    chmod 600 "$ENV"
    echo "токен: записан в .env в корне репозитория"
  else
    echo "токен: в $OLD/bot_token.md не нашёлся — запиши NARRATOR_TELEGRAM_TOKEN=… в .env сам" >&2
  fi
fi

echo
echo "Готово. Проверь: ./narrate news where --date yesterday, ./narrate bot channels."
echo "Старый каталог больше не нужен платформе — удалить его можно, когда убедишься,"
echo "что всё на месте (du -sh $HERE/library против du -sh $OLD/library)."
