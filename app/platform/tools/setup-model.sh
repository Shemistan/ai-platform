#!/bin/sh
# Ставит пайплайны и качает веса моделей фото, видео, диктора и расшифровки монтажа. Соседние репозитории
# (photogen, videogen) не нужны: всё, что нужно, — в этом скрипте.
#
# Запуск из app/platform:  make models M="z-image-turbo ltx-2.5-q4"
#   или напрямую:          sh tools/setup-model.sh <модель> [<модель>…]
#   список моделей:        sh tools/setup-model.sh --list
#   перенос весов:         make move-models TO=~/local_models
#     (= sh tools/setup-model.sh --move-to <каталог>)
# Повторный запуск докачивает недостающее; уже поставленное пропускается.
#
# Каталог весов: AIP_MODELS_DIR, иначе записанный в local.mk, иначе
#   /Volumes/External SSD/local_models (как у сервиса).
# Каталоги пайплайнов: PHOTOGEN_HOME (~/.local/share/photogen) и
#   VIDEOGEN_HOME (~/.local/share/videogen) — те же, что у старых setup-model.sh,
#   поэтому уже поставленное ими не качается заново.
# Закрытые лицензией веса (FLUX.2, Ideogram 4, LTX-2.5): токен Hugging Face в HF_TOKEN.
# Для Flux2CLI докладывается mlx.metallib (в релизном zip его нет), для FLUX.2 Klein
# заранее качается текстовый энкодер — иначе Flux2CLI тянет его на первой генерации.
# В конце пути к пайплайнам и каталог весов дописываются в local.mk (AIP_*);
# остальные строки local.mk не трогаются.
set -e
cd "$(dirname "$0")/.." || exit 1  # app/platform

# Версии пайплайнов. Вкладка «Обновления» подсказывает поднимать их здесь.
MFLUX_VERSION=0.22.0
FLUX2_VERSION=v3.1.0
FLUX2_URL="https://github.com/VincentGourbin/flux-2-swift-mlx/releases/download/$FLUX2_VERSION/Flux2CLI-$FLUX2_VERSION-macOS.zip"
LTX_VERSION=v0.16.2
# Запускатель Wan лежит в репозитории весов; ревизия закреплена — это исполняемый код.
WAN_REVISION=86bfc9847512a9eb66886388b8b92ba81e0b6729
GEMMA_REPO=mlx-community/gemma-3-12b-it-4bit
# Flux2CLI v3.1.0 собран с mlx-swift 0.31.6 = MLX v0.31.1; шейдеры — из колеса mlx-metal той же версии.
MLX_METAL_VERSION=0.31.1
# SadTalker (вкладка «Диктор»): проект не развивается с 2023 года — ревизия и окружение закреплены.
# На Mac он идёт на CPU; Python 3.10 и torch 2.0 — последние, с которыми встают его зависимости.
SADTALKER_REVISION=cd4c0465ae0b54a6f85af57f5c65fec9fe23e7f8
SADTALKER_RELEASE=https://github.com/OpenTalker/SadTalker/releases/download/v0.0.2-rc
SADTALKER_TORCH="torch==2.0.1 torchvision==0.15.2 torchaudio==2.0.2"
# Расшифровка монтажа (вкладка «Монтаж»): Whisper и Qwen3-ASR на mlx-audio в своём окружении.
MLX_AUDIO_VERSION=0.5.8
# Моушн-графика монтажа: HyperFrames CLI (Apache-2.0) — HTML + GSAP → прозрачный MOV; нужен Node 22+.
HYPERFRAMES_VERSION=0.8.172

DEFAULT_MODELS_DIR="/Volumes/External SSD/local_models"
PHOTO_HOME="${PHOTOGEN_HOME:-$HOME/.local/share/photogen}"
VIDEO_HOME="${VIDEOGEN_HOME:-$HOME/.local/share/videogen}"
MFLUX_ENV="$PHOTO_HOME/mflux-$MFLUX_VERSION"
FLUX2_BIN="$PHOTO_HOME/flux2cli-$FLUX2_VERSION/Flux2CLI"
LTX_SRC="$VIDEO_HOME/ltx-2-mlx-$LTX_VERSION"
WAN_SRC="$VIDEO_HOME/wan-video-mlx-$(printf %.7s "$WAN_REVISION")"
AVATAR_HOME="${AVATAR_HOME:-$HOME/.local/share/avatar}"
SADTALKER_SRC="$AVATAR_HOME/sadtalker-$(printf %.7s "$SADTALKER_REVISION")"
MONTAGE_HOME="${MONTAGE_HOME:-$HOME/.local/share/montage}"
ASR_ENV="$MONTAGE_HOME/mlx-audio-$MLX_AUDIO_VERSION/.venv"
HYPERFRAMES_HOME="$MONTAGE_HOME/hyperframes-$HYPERFRAMES_VERSION"
HYPERFRAMES_BIN="$HYPERFRAMES_HOME/node_modules/@hyperframes/cli/bin/hyperframes.mjs"
GEMMA_NAME="${GEMMA_REPO#*/}"
GEMMA_CACHE="${HF_HOME:-$HOME/.cache/huggingface}/hub/models--mlx-community--$GEMMA_NAME"
SETTINGS=local.mk
PYTHON="${PYTHON:-python3}"
VIDEO_IDS="ltx-2.3-q4 ltx-2.5-q4 wan-2.2-t2v-q4"
AVATAR_IDS="sadtalker"

fail() { echo "⛔ setup-model: $1" >&2; exit 1; }

# Каталог фото — тот же, что у сервиса: id пайплайн каталог отметка источник квантизация.
PHOTO_CATALOG="$("$PYTHON" -m aiplatform.photo.models)" ||
  fail "не удалось прочитать каталог моделей фото (нужен Python 3.11+: make models PYTHON=python3.12 …)"
PHOTO_IDS="$(echo "$PHOTO_CATALOG" | cut -d' ' -f1 | tr '\n' ' ' | sed 's/ $//')"
# Каталог расшифровки — тот же, что у сервиса: id репозиторий ревизия [репозиторий ревизия] (montage/asr.py).
ASR_CATALOG="$("$PYTHON" -m aiplatform.montage.asr)" || fail "не удалось прочитать каталог моделей расшифровки"
ASR_IDS="$(echo "$ASR_CATALOG" | cut -d' ' -f1 | tr '\n' ' ' | sed 's/ $//')"
MOTION_IDS="hyperframes"

# Строка KEY=VALUE в local.mk: старая с тем же ключом убирается, остальное — как было.
set_setting() {
  touch "$SETTINGS"
  grep -v "^$1=" "$SETTINGS" > "$SETTINGS.tmp" || true
  echo "$1=$2" >> "$SETTINGS.tmp"
  mv "$SETTINGS.tmp" "$SETTINGS"
}

SAVED_DIR="$(sed -n 's/^AIP_MODELS_DIR=//p' "$SETTINGS" 2>/dev/null | tail -1)"
MODELS_DIR="${AIP_MODELS_DIR:-${SAVED_DIR:-$DEFAULT_MODELS_DIR}}"

# Перенос всех весов на другой диск: каталоги переезжают целиком (mv), local.mk
# получает новый AIP_MODELS_DIR. Совпадающие имена в цели — отказ до переноса.
move_models() {
  to="$1"
  [ -n "$to" ] || fail "куда переносить? sh tools/setup-model.sh --move-to <каталог>"
  [ -d "$MODELS_DIR" ] || fail "каталога весов $MODELS_DIR нет — диск подключён?"
  mkdir -p "$to" 2>/dev/null || fail "не могу создать $to"
  to="$(cd "$to" && pwd)" from="$(cd "$MODELS_DIR" && pwd)"
  [ "$from" != "$to" ] || fail "веса уже в $to"
  case "$to/" in "$from"/*) fail "$to внутри $from — выбери каталог снаружи" ;; esac
  clash="$(find "$from" -mindepth 1 -maxdepth 1 -exec basename {} \; | while read -r name; do
    [ ! -e "$to/$name" ] || echo "$name"
  done)"
  [ -z "$clash" ] || fail "в $to уже есть: $(echo "$clash" | tr '\n' ' ')— убери или выбери другой каталог"
  echo "→ переношу веса: $from → $to"
  find "$from" -mindepth 1 -maxdepth 1 -exec mv {} "$to/" \;
  rmdir "$from" 2>/dev/null || true
  set_setting AIP_MODELS_DIR "$to"
  echo "✓ веса в $to, AIP_MODELS_DIR записан в $(pwd)/$SETTINGS"
}

if [ "$1" = "--move-to" ]; then
  move_models "$2"
  exit 0
fi

if [ "$1" = "--list" ]; then
  echo "фото: $PHOTO_IDS"
  echo "видео: $VIDEO_IDS"
  echo "диктор: $AVATAR_IDS"
  echo "расшифровка: $ASR_IDS"
  echo "моушн-графика: $MOTION_IDS"
  exit 0
fi

ALL_IDS="фото: $PHOTO_IDS; видео: $VIDEO_IDS; диктор: $AVATAR_IDS; расшифровка: $ASR_IDS; моушн-графика: $MOTION_IDS"
[ $# -gt 0 ] || fail "укажи модель. $ALL_IDS"
for id in "$@"; do
  case " $PHOTO_IDS $VIDEO_IDS $AVATAR_IDS $ASR_IDS $MOTION_IDS " in
    *" $id "*) ;;
    *) fail "неизвестная модель «${id}». $ALL_IDS" ;;
  esac
done

[ "$(uname -sm)" = "Darwin arm64" ] || fail "MLX работает только на Mac с Apple Silicon"
command -v uv >/dev/null 2>&1 || fail "нужен uv: brew install uv"
mkdir -p "$MODELS_DIR" 2>/dev/null || fail "не могу создать $MODELS_DIR — внешний диск подключён?
  Другой каталог: make models M=\"$*\" AIP_MODELS_DIR=~/local_models"

write_settings() {
  set_setting AIP_MODELS_DIR "$MODELS_DIR"
  [ ! -x "$MFLUX_ENV/bin/mflux-generate-qwen" ] || set_setting AIP_PHOTO_MFLUX_BIN "$MFLUX_ENV/bin"
  [ ! -x "$FLUX2_BIN" ] || set_setting AIP_PHOTO_FLUX2_CMD "\"$FLUX2_BIN\""
  [ ! -f "$LTX_SRC/pyproject.toml" ] || set_setting AIP_VIDEO_LTX_CMD "uv run --project $LTX_SRC ltx-2-mlx"
  [ ! -f "$WAN_SRC/pyproject.toml" ] || set_setting AIP_VIDEO_WAN_CMD "uv run --project $WAN_SRC pocketai-wan-video"
  [ ! -x "$SADTALKER_SRC/.venv/bin/python" ] || set_setting AIP_AVATAR_SADTALKER_HOME "$SADTALKER_SRC"
  [ ! -x "$ASR_ENV/bin/hf" ] || set_setting AIP_MONTAGE_ASR_PYTHON "$ASR_ENV/bin/python"
  # Полный путь к node: под launchd PATH короткий, а nvm-ноды в нём нет.
  [ ! -f "$HYPERFRAMES_BIN" ] || set_setting AIP_MONTAGE_HYPERFRAMES_CMD "\"$(command -v node)\" \"$HYPERFRAMES_BIN\""
}

token_help() {
  echo "  Веса закрыты лицензией. Один раз нужно:
  1. открыть $1, войти и принять условия;
  2. создать токен типа Read: https://huggingface.co/settings/tokens
  3. запустить снова:  HF_TOKEN=hf_… make models M=$2
  Токен — как пароль: в файлы проекта его не клади."
}

# --- фото: mflux (Qwen, Z-Image, Krea, Ideogram) и Flux2CLI (FLUX.2) ---

install_mflux() {
  [ ! -x "$MFLUX_ENV/bin/mflux-generate-qwen" ] || return 0
  echo "→ ставлю mflux $MFLUX_VERSION в $MFLUX_ENV"
  mkdir -p "$PHOTO_HOME"
  uv venv --quiet --python 3.12 "$MFLUX_ENV"
  uv pip install --quiet --python "$MFLUX_ENV/bin/python" "mflux==$MFLUX_VERSION"
}

install_flux2() {
  [ ! -x "$FLUX2_BIN" ] || return 0
  echo "→ скачиваю Flux2CLI $FLUX2_VERSION"
  dir="$(dirname "$FLUX2_BIN")"
  mkdir -p "$dir"
  curl -fsSL "$FLUX2_URL" -o "$dir/cli.zip" || fail "Flux2CLI не скачался: $FLUX2_URL"
  unzip -q -o "$dir/cli.zip" -d "$dir"
  rm -f "$dir/cli.zip"
  chmod +x "$FLUX2_BIN"
  # Бинарь не подписан: без этого Gatekeeper может не дать его запустить.
  xattr -d com.apple.quarantine "$FLUX2_BIN" 2>/dev/null || true
}

# Релизный zip Flux2CLI — только бинарник; без mlx.metallib рядом FLUX.2 падает на
# первой генерации («Failed to load the default metallib»). Берём файл из колеса
# mlx-metal (или готовый из MLX_METALLIB) и кладём рядом с бинарником.
ensure_metallib() {
  dest="$(dirname "$FLUX2_BIN")/mlx.metallib"
  [ ! -f "$dest" ] || return 0
  if [ -n "$MLX_METALLIB" ]; then
    [ -f "$MLX_METALLIB" ] || fail "MLX_METALLIB=$MLX_METALLIB — файла нет"
    cp "$MLX_METALLIB" "$dest"
  else
    echo "→ докладываю mlx.metallib (mlx-metal==$MLX_METAL_VERSION) к Flux2CLI"
    tmp="$(mktemp -d)"
    uv pip install --quiet --target "$tmp" "mlx-metal==$MLX_METAL_VERSION" ||
      fail "mlx-metal==$MLX_METAL_VERSION не скачался — нужна сеть к PyPI"
    found="$(find "$tmp" -name mlx.metallib | head -1)"
    [ -n "$found" ] || fail "в колесе mlx-metal==$MLX_METAL_VERSION нет mlx.metallib"
    cp "$found" "$dest"
    rm -rf "$tmp"
  fi
  echo "✓ mlx.metallib рядом с Flux2CLI"
}

# Текстовый энкодер Klein: Flux2CLI (swift-transformers) держит его рядом с --models-dir —
# в AIP_MODELS_DIR/models/<repo> — и без него качает ~5 ГБ на первой генерации.
preload_encoder() {
  repo="$1"
  [ "$repo" != "-" ] || return 0
  target="$MODELS_DIR/models/$repo"
  ! ls "$target"/*.safetensors >/dev/null 2>&1 || return 0
  install_mflux  # в окружении mflux есть hf
  echo "→ скачиваю текстовый энкодер $repo в $target"
  HF_HOME="$MODELS_DIR/.photogen-cache/hf" "$MFLUX_ENV/bin/hf" download "$repo" --local-dir "$target" >/dev/null ||
    fail "энкодер $repo не скачался (нужна сеть к Hugging Face)"
}

install_photo() {
  # shellcheck disable=SC2046
  set -- $(echo "$PHOTO_CATALOG" | grep "^$1 ")
  id="$1" backend="$2" dir="$MODELS_DIR/$3" marker="$MODELS_DIR/$3/$4" source="$5" quant="$6" encoder="$7"
  if [ -f "$marker" ]; then
    # Поставленной раньше FLUX.2 докладываем то, чего старый скрипт не ставил.
    if [ "$backend" = "flux2" ]; then
      install_flux2
      ensure_metallib
      preload_encoder "$encoder"
    fi
    echo "✓ $id уже установлена"
    return 0
  fi
  # Всё, что пайплайны кэшируют сами, ложится рядом с весами — как у сервиса при генерации.
  cache="$MODELS_DIR/.photogen-cache"
  if [ "$backend" = "mflux" ]; then
    install_mflux
    echo "→ скачиваю веса $source в $dir"
    HF_HOME="$cache/hf" MFLUX_CACHE_DIR="$cache/mflux" "$MFLUX_ENV/bin/hf" download "$source" --local-dir "$dir" >/dev/null || {
      write_settings
      fail "веса $source не скачались.
$(token_help "https://huggingface.co/$source" "$id")"
    }
  else
    install_flux2
    ensure_metallib
    case "$source" in
      dev) page=https://huggingface.co/black-forest-labs/FLUX.2-dev ;;
      *) page=https://huggingface.co/black-forest-labs/FLUX.2-klein-9B ;;
    esac
    [ -n "$HF_TOKEN" ] || {
      write_settings
      fail "для $id нужен токен Hugging Face в HF_TOKEN.
$(token_help "$page" "$id")"
    }
    echo "→ скачиваю веса FLUX.2 ($source, $quant) в $dir"
    # Токен Flux2CLI берёт из HF_TOKEN сам — в аргументы его не передаём.
    HF_HOME="$cache/hf" "$FLUX2_BIN" download --model "$source" --transformer-quant "$quant" --models-dir "$dir" || {
      write_settings
      fail "веса $id не скачались.
$(token_help "$page" "$id")"
    }
    preload_encoder "$encoder"
  fi
  touch "$marker"
  echo "✓ $id установлена"
}

# --- видео: ltx-2-mlx (LTX, режим --distilled) и запускатель Wan (Q4) ---

install_ltx() {
  if [ ! -f "$LTX_SRC/pyproject.toml" ]; then
    echo "→ скачиваю ltx-2-mlx $LTX_VERSION"
    mkdir -p "$LTX_SRC"
    curl -fsSL "https://github.com/dgrauet/ltx-2-mlx/archive/refs/tags/$LTX_VERSION.tar.gz" |
      tar -xz -C "$LTX_SRC" --strip-components=1
  fi
  echo "→ ставлю зависимости ltx-2-mlx (uv sync)"
  (cd "$LTX_SRC" && uv sync --quiet)
}

install_video() {
  id="$1" revision="" need_gemma=""
  # Файлы для --distilled у каждой модели свои; текстовый энкодер у 2.5 лежит в самом репозитории.
  case "$id" in
    wan-2.2-t2v-q4)
      repo=PocketAiHub/Wan2.2-T2V-A14B-MLX
      revision="$WAN_REVISION"
      files="common/* q4/* src/* pyproject.toml README.md LICENSE*" ;;
    ltx-2.3-q4)
      repo=dgrauet/ltx-2.3-mlx-q4
      need_gemma=1
      files="transformer-distilled-1.1.safetensors connector.safetensors vae_encoder.safetensors
        vae_decoder.safetensors audio_vae.safetensors vocoder.safetensors spatial_upscaler_x2_v1_1.safetensors" ;;
    ltx-2.5-q4)
      repo=dgrauet/ltx-2.5-mlx-q4
      files="transformer-distilled.safetensors connector.safetensors text_encoder.safetensors
        duration_head.safetensors vae_encoder_conv.safetensors vae_decoder_conv.safetensors audio_vae.safetensors
        vocoder.safetensors spatial_upscaler_x2_v1_0.safetensors chat_template.jinja" ;;
  esac
  dir="$MODELS_DIR/${repo#*/}"
  command -v ffmpeg >/dev/null 2>&1 || fail "нужен ffmpeg: brew install ffmpeg-full"
  install_ltx  # в его окружении есть hf — им и качаем веса

  # В репозиториях лежат и трансформеры других режимов, и LoRA — десятки лишних
  # гигабайт; качаем только файлы режима --distilled. FULL=1 — весь репозиторий.
  echo "→ скачиваю веса $repo в $dir"
  set -- "$repo" --local-dir "$dir"
  [ -z "$revision" ] || set -- "$@" --revision "$revision"
  if [ -z "$FULL" ]; then
    set -- "$@" --include "*.json"
    set -f  # маски идут в hf как есть, оболочка их не раскрывает
    for f in $files; do set -- "$@" --include "$f"; done
    set +f
  fi
  (cd "$LTX_SRC" && uv run hf download "$@") || {
    write_settings
    fail "веса $repo не скачались.
$(token_help "https://huggingface.co/$repo" "$id")"
  }
  # Энкодер LTX-2.3 кладём рядом с весами; уже скачанный в кэш Hugging Face не дублируем.
  if [ -n "$need_gemma" ] && [ ! -d "$MODELS_DIR/$GEMMA_NAME" ] && [ ! -d "$GEMMA_CACHE/snapshots" ]; then
    echo "→ скачиваю текстовый энкодер $GEMMA_REPO"
    (cd "$LTX_SRC" && uv run hf download "$GEMMA_REPO" --local-dir "$MODELS_DIR/$GEMMA_NAME" >/dev/null)
  fi
  # Запускатель Wan: копия исходников из каталога весов и своё окружение uv.
  if [ "$id" = wan-2.2-t2v-q4 ]; then
    echo "→ ставлю запускатель Wan в $WAN_SRC"
    mkdir -p "$WAN_SRC"
    cp -R "$dir/src" "$dir/pyproject.toml" "$dir/README.md" "$dir"/LICENSE* "$WAN_SRC/"
    (cd "$WAN_SRC" && uv sync --quiet) || fail "зависимости запускателя Wan не встали (uv sync в $WAN_SRC)"
  fi
  echo "✓ $id установлена"
}

# --- диктор: SadTalker (фото + речь → говорящая голова) ---

# Файл из релиза — докачкой во временный и переименованием: оборванная загрузка не выглядит готовой.
fetch() {
  [ ! -s "$2" ] || return 0
  echo "→ скачиваю $(basename "$2")"
  curl -fL --retry 3 -C - -sS "$1" -o "$2.part" || fail "не скачался $1"
  mv "$2.part" "$2"
}

install_sadtalker() {
  dir="$MODELS_DIR/SadTalker"
  command -v ffmpeg >/dev/null 2>&1 || fail "нужен ffmpeg: brew install ffmpeg-full"
  if [ ! -f "$SADTALKER_SRC/inference.py" ]; then
    echo "→ скачиваю SadTalker $(printf %.7s "$SADTALKER_REVISION") в $SADTALKER_SRC"
    mkdir -p "$SADTALKER_SRC"
    curl -fsSL "https://github.com/OpenTalker/SadTalker/archive/$SADTALKER_REVISION.tar.gz" |
      tar -xz -C "$SADTALKER_SRC" --strip-components=1 || fail "исходники SadTalker не скачались"
  fi
  py="$SADTALKER_SRC/.venv/bin/python"
  # Проверка — импорт самих модулей SadTalker, а не пакетов: зависимость может встать и не импортироваться
  # (librosa 0.9 берёт pkg_resources, которого нет в свежем setuptools).
  check="import sys; sys.path.insert(0, '$SADTALKER_SRC'); import src.generate_batch, src.facerender.animate, src.utils.preprocess"
  if ! "$py" -c "$check" 2>/dev/null; then
    echo "→ ставлю окружение SadTalker (Python 3.10, $SADTALKER_TORCH)"
    uv venv --quiet --allow-existing --python 3.10 "$SADTALKER_SRC/.venv" || fail "uv не нашёл Python 3.10"
    # shellcheck disable=SC2086
    uv pip install --quiet --python "$py" $SADTALKER_TORCH "setuptools<70" wheel || fail "torch для SadTalker не встал"
    # gradio — только для их веб-демо; basicsr собирается из исходников и импортирует torch при сборке.
    grep -v '^gradio' "$SADTALKER_SRC/requirements.txt" | sed 's/ *#.*//' > "$SADTALKER_SRC/requirements-aip.txt"
    # shellcheck disable=SC2086
    uv pip install --quiet --python "$py" --no-build-isolation $SADTALKER_TORCH "setuptools<70" -r "$SADTALKER_SRC/requirements-aip.txt" ||
      fail "зависимости SadTalker не встали (uv pip install в $SADTALKER_SRC/.venv)"
    "$py" -c "$check" || fail "SadTalker не импортируется в своём окружении — вывод выше"
  fi
  mkdir -p "$dir/checkpoints" "$dir/gfpgan/weights"
  for f in mapping_00109-model.pth.tar mapping_00229-model.pth.tar SadTalker_V0.0.2_256.safetensors \
    SadTalker_V0.0.2_512.safetensors; do
    fetch "$SADTALKER_RELEASE/$f" "$dir/checkpoints/$f"
  done
  # Детектор и выравнивание лиц, улучшение лица: SadTalker ищет их в gfpgan/weights от рабочего каталога.
  fetch https://github.com/xinntao/facexlib/releases/download/v0.1.0/alignment_WFLW_4HG.pth "$dir/gfpgan/weights/alignment_WFLW_4HG.pth"
  fetch https://github.com/xinntao/facexlib/releases/download/v0.1.0/detection_Resnet50_Final.pth \
    "$dir/gfpgan/weights/detection_Resnet50_Final.pth"
  fetch https://github.com/TencentARC/GFPGAN/releases/download/v1.3.0/GFPGANv1.4.pth "$dir/gfpgan/weights/GFPGANv1.4.pth"
  fetch https://github.com/xinntao/facexlib/releases/download/v0.2.2/parsing_parsenet.pth "$dir/gfpgan/weights/parsing_parsenet.pth"
  echo "✓ sadtalker установлена"
}

# --- расшифровка монтажа: mlx-audio (Whisper, Qwen3-ASR + ForcedAligner) ---

install_asr_env() {
  [ ! -x "$ASR_ENV/bin/hf" ] || return 0
  echo "→ ставлю mlx-audio $MLX_AUDIO_VERSION в $ASR_ENV"
  mkdir -p "$(dirname "$ASR_ENV")"
  uv venv --quiet --allow-existing --python 3.12 "$ASR_ENV" || fail "uv не нашёл Python 3.12"
  uv pip install --quiet --python "$ASR_ENV/bin/python" "mlx-audio[stt]==$MLX_AUDIO_VERSION" ||
    fail "mlx-audio==$MLX_AUDIO_VERSION не встал (нужна сеть к PyPI)"
}

# Веса с закреплённой ревизией (SEC-011) в AIP_MODELS_DIR/asr/<имя репозитория>: так их ищет сервис.
fetch_asr_weights() {
  repo="$1" revision="$2" dir="$MODELS_DIR/asr/${1#*/}"
  [ ! -f "$dir/.aip-$revision" ] || return 0
  echo "→ скачиваю $repo в $dir"
  HF_HOME="$MODELS_DIR/asr/.cache" "$ASR_ENV/bin/hf" download "$repo" --revision "$revision" --local-dir "$dir" >/dev/null ||
    fail "веса $repo не скачались (нужна сеть к Hugging Face)"
  touch "$dir/.aip-$revision"
}

# Whisper: токенизатор и признаки звука (WhisperProcessor) из openai/whisper-* — в папку весов
# mlx-community, где их ищет mlx-audio; без них расшифровка падает на первом куске (BE-015).
# Проверка — загрузкой процессора: она не трогает GPU и ловит неполный набор файлов.
fetch_asr_processor() {
  id="$1" weights_repo="$2"
  # shellcheck disable=SC2046
  set -- $("$PYTHON" -m aiplatform.montage.asr processor "$id")
  [ $# -gt 0 ] || return 0
  repo="$1" revision="$2" dir="$MODELS_DIR/asr/${weights_repo#*/}"
  shift 2
  if [ ! -f "$dir/.aip-processor-$revision" ]; then
    echo "→ скачиваю токенизатор $repo в $dir"
    HF_HOME="$MODELS_DIR/asr/.cache" "$ASR_ENV/bin/hf" download "$repo" "$@" --revision "$revision" \
      --local-dir "$dir" >/dev/null || fail "токенизатор $repo не скачался (нужна сеть к Hugging Face)"
    touch "$dir/.aip-processor-$revision"
  fi
  "$ASR_ENV/bin/python" -I -c 'import sys; from transformers import WhisperProcessor; WhisperProcessor.from_pretrained(sys.argv[1])' \
    "$dir" 2>/dev/null || fail "WhisperProcessor не загрузился из $dir — удали $dir/.aip-processor-$revision и повтори"
}

install_asr() {
  # shellcheck disable=SC2046
  set -- $(echo "$ASR_CATALOG" | grep "^$1 ")
  id="$1"
  install_asr_env
  fetch_asr_weights "$2" "$3"
  [ -z "$4" ] || fetch_asr_weights "$4" "$5"
  fetch_asr_processor "$id" "$2"
  echo "✓ $id установлена"
}

# --- моушн-графика монтажа: HyperFrames CLI ---

install_hyperframes() {
  command -v node >/dev/null 2>&1 || fail "нужен Node 22+: brew install node@22 (или nvm install 22)"
  major="$(node -p 'process.versions.node.split(".")[0]')"
  [ "$major" -ge 22 ] || fail "HyperFrames нужен Node 22+, сейчас $(node --version): brew install node@22 или nvm install 22"
  if [ ! -f "$HYPERFRAMES_BIN" ]; then
    echo "→ ставлю HyperFrames $HYPERFRAMES_VERSION в $HYPERFRAMES_HOME (с ним — headless Chrome)"
    mkdir -p "$HYPERFRAMES_HOME"
    npm install --silent --prefix "$HYPERFRAMES_HOME" "@hyperframes/cli@$HYPERFRAMES_VERSION" ||
      fail "@hyperframes/cli@$HYPERFRAMES_VERSION не встал (нужна сеть к npm)"
  fi
  node "$HYPERFRAMES_BIN" doctor || echo "⚠ hyperframes doctor недоволен — вывод выше"
  echo "✓ hyperframes установлен"
}

for id in "$@"; do
  if echo " $VIDEO_IDS " | grep -q " $id "; then
    install_video "$id"
  elif echo " $AVATAR_IDS " | grep -q " $id "; then
    install_sadtalker
  elif echo " $ASR_IDS " | grep -q " $id "; then
    install_asr "$id"
  elif [ "$id" = hyperframes ]; then
    install_hyperframes
  else
    install_photo "$id"
  fi
done

write_settings
echo "✓ готово. Настройки записаны в $(pwd)/$SETTINGS — запускай: make run"
