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

hf() { uvx --from huggingface_hub hf download "$@"; }

if [ ! -s "$MODELS/silero/v5_5_ru.pt" ]; then
  echo "== Silero TTS v5.5 (движок silero, лицензия CC BY-NC)"
  mkdir -p "$MODELS/silero"
  curl -L --fail -o "$MODELS/silero/v5_5_ru.pt.part" https://models.silero.ai/models/tts/ru/v5_5_ru.pt
  mv "$MODELS/silero/v5_5_ru.pt.part" "$MODELS/silero/v5_5_ru.pt"
fi

echo "== RUAccent turbo3.1 (ударения и омографы) и морфоанализатор koziev"
hf ruaccent/accentuator \
  --include "dictionary/*" "nn/nn_accent/*" "nn/nn_omograph/turbo3.1/*" \
  "nn/nn_stress_usage_predictor/*" "nn/nn_yo_homograph_resolver/*" "koziev/*" \
  --local-dir "$MODELS/ruaccent"

if [ ! -d "$MODELS/supertonic3/onnx" ]; then
  echo "== Supertonic 3 (движок supertonic)"
  NARRATOR_MODELS_DIR="$MODELS" "$(dirname "$0")/../narrate" setup
fi

echo "== faster-whisper small (проверка озвучки распознаванием)"
hf Systran/faster-whisper-small --local-dir "$MODELS/faster-whisper-small"

if [ "$1" != "--no-espeech" ]; then
  echo "== ESpeech-TTS-1 RL-V2 (F5-TTS для русского, 2,7 ГБ) и вокодер"
  hf ESpeech/ESpeech-TTS-1_RL-V2 --local-dir "$MODELS/ESpeech-TTS-1_RL-V2"
  hf charactr/vocos-mel-24khz --local-dir "$MODELS/vocos-mel-24khz"
fi

echo "Готово. Скажи агенту, что модели скачаны."
