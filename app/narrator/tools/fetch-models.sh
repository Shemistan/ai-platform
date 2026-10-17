#!/bin/sh
# Скачивает модели русской озвучки в общую папку моделей. Запускать в обычном
# терминале: sandbox агента не пускает к models.silero.ai и huggingface.co.
# Всего около 3,7 ГБ, дальше всё работает офлайн. Повторный запуск докачивает
# только недостающее.
#   sh tools/fetch-models.sh              — всё
#   sh tools/fetch-models.sh --no-espeech — без ESpeech и его вокодера (2,7 ГБ)
# Папка — $NARRATOR_MODELS_DIR, по умолчанию внешний диск (src/narrator/models.py).
set -e

MODELS="${NARRATOR_MODELS_DIR:-/Volumes/External SSD/local_models}"
if [ ! -d "$MODELS" ]; then
  echo "нет папки моделей: $MODELS — подключи диск или задай NARRATOR_MODELS_DIR" >&2
  exit 1
fi
echo "папка моделей: $MODELS"

# Версии закреплены (SEC-011): huggingface_hub — как в uv.lock, у каждой модели —
# коммит репозитория (--revision), у Silero — sha256 файла. Подмена файла на
# зеркале или в репозитории модели не дойдёт до загрузки: веса — исполняемый код.
HF_HUB_VERSION=1.32.0
# Как SILERO_SHA256 в src/narrator/synth.py.
SILERO_SHA256=50081637b602126ee06cb3bc8a744d25651d2da149ee8864b9a379bfdd934437
REV_RUACCENT=b78ae5ea1e62beaf138bed1865cd8c3b0b5ca855
REV_WHISPER=536b0662742c02347bc0e980a01041f333bce120
REV_ESPEECH=f582b6e5897fe8a5835059405a8439d13bdf7684
REV_VOCOS=0feb3fdd929bcd6649e0e7c5a688cf7dd012ef21

hf() { uvx --from "huggingface_hub==$HF_HUB_VERSION" hf download "$@"; }

# check_sha256 <файл> <сумма>: не совпала — файл удаляется, скрипт падает.
check_sha256() {
  if ! echo "$2  $1" | shasum -a 256 -c - >/dev/null 2>&1; then
    rm -f "$1"
    echo "контрольная сумма $1 не совпала с ожидаемой — файл удалён; скачай заново" >&2
    exit 1
  fi
}

if [ ! -s "$MODELS/silero/v5_5_ru.pt" ]; then
  echo "== Silero TTS v5.5 (движок silero, лицензия CC BY-NC)"
  mkdir -p "$MODELS/silero"
  curl -L --fail -o "$MODELS/silero/v5_5_ru.pt.part" https://models.silero.ai/models/tts/ru/v5_5_ru.pt
  check_sha256 "$MODELS/silero/v5_5_ru.pt.part" "$SILERO_SHA256"
  mv "$MODELS/silero/v5_5_ru.pt.part" "$MODELS/silero/v5_5_ru.pt"
fi

echo "== RUAccent turbo3.1 (ударения и омографы) и морфоанализатор koziev"
hf ruaccent/accentuator --revision "$REV_RUACCENT" \
  --include "dictionary/*" "nn/nn_accent/*" "nn/nn_omograph/turbo3.1/*" \
  "nn/nn_stress_usage_predictor/*" "nn/nn_yo_homograph_resolver/*" "koziev/*" \
  --local-dir "$MODELS/ruaccent"

if [ ! -d "$MODELS/supertonic3/onnx" ]; then
  echo "== Supertonic 3 (движок supertonic)"
  NARRATOR_MODELS_DIR="$MODELS" "$(dirname "$0")/../narrate" setup
fi

echo "== faster-whisper small (проверка озвучки распознаванием)"
hf Systran/faster-whisper-small --revision "$REV_WHISPER" --local-dir "$MODELS/faster-whisper-small"

if [ "$1" != "--no-espeech" ]; then
  echo "== ESpeech-TTS-1 RL-V2 (F5-TTS для русского, 2,7 ГБ) и вокодер"
  hf ESpeech/ESpeech-TTS-1_RL-V2 --revision "$REV_ESPEECH" --local-dir "$MODELS/ESpeech-TTS-1_RL-V2"
  hf charactr/vocos-mel-24khz --revision "$REV_VOCOS" --local-dir "$MODELS/vocos-mel-24khz"
fi

echo "Готово. Скажи агенту, что модели скачаны."
