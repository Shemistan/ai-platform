#!/bin/sh
# Переносит модели озвучки из ~/.cache в общую папку моделей на внешнем диске.
# Запускать в обычном терминале: sandbox агента не пишет ни туда, ни туда.
#   sh tools/move-models.sh
# rsync убирает со старого места только те файлы, что уже целиком легли на
# новое; повторный запуск доносит оставшееся. Чего в ~/.cache нет
# (faster-whisper, вокодер) — докачает sh tools/fetch-models.sh.
set -e

MODELS="${NARRATOR_MODELS_DIR:-/Volumes/External SSD/local_models}"
CACHE="$HOME/.cache"
if [ ! -d "$MODELS" ]; then
  echo "нет папки моделей: $MODELS — подключи диск или задай NARRATOR_MODELS_DIR" >&2
  exit 1
fi

# move <откуда> <куда>: каталог или файл.
move() {
  src=$1
  dst=$2
  if [ ! -e "$src" ]; then
    echo "-- нет $src — пропускаю"
    return 0
  fi
  echo "== $src → $dst"
  mkdir -p "$(dirname "$dst")"
  if [ -d "$src" ]; then
    mkdir -p "$dst"
    rsync -a --remove-source-files "$src/" "$dst/"
    find "$src" -depth -type d -empty -delete
  else
    rsync -a --remove-source-files "$src" "$dst"
  fi
}

move "$CACHE/espeech/ESpeech-TTS-1_RL-V2" "$MODELS/ESpeech-TTS-1_RL-V2"
rmdir "$CACHE/espeech" 2>/dev/null || true
move "$CACHE/ruaccent" "$MODELS/ruaccent"
move "$CACHE/supertonic3" "$MODELS/supertonic3"
move "$CACHE/torch/hub/snakers4_silero-models_master/src/silero/model/v5_5_ru.pt" \
  "$MODELS/silero/v5_5_ru.pt"

echo
echo "В $MODELS:"
du -sh "$MODELS/ESpeech-TTS-1_RL-V2" "$MODELS/ruaccent" "$MODELS/supertonic3" "$MODELS/silero" 2>/dev/null || true
for need in faster-whisper-small/model.bin vocos-mel-24khz/config.yaml; do
  [ -e "$MODELS/$need" ] || echo "нет $MODELS/$need — докачай: sh tools/fetch-models.sh"
done
