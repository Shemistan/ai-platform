# Аудит окружения (MASTER_SPEC §0, P0)

Подтверждённые факты на машине автора, 2026-10-19. Повторить: команды в
правом столбце. Живой реестр с probe — `GET /api/montage/capabilities`.

## Железо и ОС

| Что | Факт | Как проверено |
|---|---|---|
| Машина | MacBook Pro, Apple M1 Pro, 10 ядер CPU, 16 ядер GPU, 32 ГБ | `system_profiler SPHardwareDataType SPDisplaysDataType` |
| ОС | macOS 27.0.1 (26A434) | `sw_vers` |
| GPU API | Metal: Supported; CUDA нет и не предполагается | то же |
| VRAM | общая память (unified), бюджет — один GPU-замок на все модели (ADR 0004) | `core/gate.py` |
| Диск | системный 69 ГБ свободно; `/Volumes/External SSD` 210 ГБ свободно (веса моделей) | `df -h` |

## Инструменты

| Инструмент | Факт | Как проверено |
|---|---|---|
| Python | 3.12.0 (`python3.12` из pyenv); сервис требует 3.11+ | `python3 --version` |
| uv | 0.12.17 | `uv --version` |
| ruff | 0.15.19 | `ruff --version` |
| ffmpeg / ffprobe | 7.1.1 (`/opt/homebrew/bin`), фильтры есть: `subtitles`, `ass`, `drawtext`, `loudnorm`, `silencedetect`, `blackdetect`, `astats`, `ebur128` | `ffmpeg -filters` |
| Node | 20.20.0 по умолчанию (nvm); 22.21.1 установлен (`~/.nvm/versions/node/v22.21.1`) — нужен HyperFrames | `node -v`, `ls ~/.nvm/versions/node` |
| HyperFrames | **не установлен** (`make models M=hyperframes`) — моушн-сцены будут `blocked`, пока не поставить | `AIP_MONTAGE_HYPERFRAMES_CMD` не найден |
| claude CLI | 2.1.303 (`~/.local/bin/claude`) — LLM через `claude -p` (ADR 0006) | `claude --version` |
| Ollama | бинарник есть, сервер не запущен; платформой не используется | `ollama list` → connection refused |
| Blender | **нет** — 3D недоступно (`unavailable`) | `which blender` |
| pdftotext (poppler) | **нет** — PDF-материалы недоступны, пока не `brew install poppler` | `which pdftotext` |

## Локальные модели (`AIP_MODELS_DIR=/Volumes/External SSD/local_models`)

| Задача | Модель | Статус |
|---|---|---|
| ASR (слова со временем) | `asr/whisper-large-v3-turbo`, `asr/whisper-large-v3-mlx` (mlx-audio 0.5.8 в `~/.local/share/montage`) | установлены |
| ASR | Qwen3-ASR 1.7B + ForcedAligner | не установлены |
| Проверка озвучки | `faster-whisper-small` | установлена (narrator) |
| Текст LLM | `claude -p` (облачная подписка, явное решение автора — ADR 0006); `gemma-3-12b-it-4bit` лежит на диске, в платформе не подключена | — |
| VLM / embeddings / диаризация / VAD-модель | нет; VAD — `silencedetect` ffmpeg; семантический QA — LLM по расшифровке | `unavailable` |
| Картинки | Z-Image Turbo, Krea-2, Qwen-Image-2512, FLUX.2 (Flux2CLI) | вкладка «Фото» |
| Видео (i2v/t2v) | LTX-2.5 Q4, LTX-2.3 Q4, Wan 2.2 | вкладка «Видео» |
| TTS | ESpeech, Silero, Supertonic, mlx-audio | вкладка «Аудио» |
| Говорящая голова | SadTalker | вкладка «Диктор» |

## Лицензии и сеть

- Шрифты: системные (SF Pro / Helvetica) и `AIP_FONT` для drawtext — лицензии
  macOS; в композициях HyperFrames — системные.
- Единственный сетевой вызов композиции — GSAP с jsdelivr (CSP, PRD-026);
  модели и claude CLI — свои сети, платформа ничего не качает сама.
- Музыки и стоков нет; ролики без музыки.

## Неизвестное

- Скорость HyperFrames-рендера на этой машине — замерить после установки.
- Точность Qwen3-ASR на русском с терминами — `make asr-compare` (PRD-020).
