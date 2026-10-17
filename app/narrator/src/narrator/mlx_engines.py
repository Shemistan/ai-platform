"""Движки для сравнения на mlx-audio: Higgs Audio v3, Fish Audio S2 Pro, MOSS-TTS,
Qwen3-TTS, Chatterbox Multilingual. Только Mac на Apple Silicon.

Все пять клонируют голос с того же образца M1, что и ESpeech, — сравнивается
модель, а не тембр. Ударения они ставят сами: RUAccent здесь не участвует,
ручные акуты из script.txt убираются.

mlx-audio живёт в отдельном окружении (ему нужен transformers 5, основному — нет):
модель грузит воркер narrator/mlx_worker.py, запущенный через `uv run --with`.
Веса лежат кэшем HuggingFace в папке моделей, подкаталог mlx-audio; скачивает
их `./narrate setup --mlx <движок>`.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from narrator import models

MLX_AUDIO = "mlx-audio[tts]==0.5.8"
WORKER_PYTHON = "3.12"
WORKER = Path(__file__).with_name("mlx_worker.py")
CACHE_DIR = "mlx-audio"
# Абзац длиннее режется по предложениям: у моделей есть предел токенов на вызов.
MAX_CHARS = 400


@dataclass(frozen=True)
class MlxEngine:
    id: str
    label: str
    repo: str
    size_gb: float
    note: str
    # Параметры model.generate(): язык у каждой модели называется по-своему.
    kwargs: dict = field(default_factory=dict)
    extra_repos: tuple[str, ...] = ()


MLX_ENGINES: dict[str, MlxEngine] = {
    e.id: e
    for e in (
        MlxEngine(
            "higgs",
            "Higgs Audio v3",
            "bosonai/higgs-audio-v3-tts-4b",
            10,
            "4B, только некоммерческая лицензия",
            {"temperature": 1.0, "max_new_tokens": 2048},
        ),
        MlxEngine(
            "fish",
            "Fish Audio S2 Pro",
            "mlx-community/fish-audio-s2-pro-bf16",
            11,
            "5B, только некоммерческая лицензия",
        ),
        MlxEngine(
            "moss",
            "MOSS-TTS v1.5",
            "OpenMOSS-Team/MOSS-TTS-v1.5",
            17,
            "8B, самая тяжёлая",
            {"language": "Russian"},
            ("OpenMOSS-Team/MOSS-Audio-Tokenizer",),
        ),
        MlxEngine(
            "qwen3",
            "Qwen3-TTS 1.7B",
            "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16",
            4.5,
            "1,7B, самая лёгкая из больших",
            {"lang_code": "russian"},
        ),
        MlxEngine(
            "chatterbox",
            "Chatterbox V3",
            "mlx-community/chatterbox-multilingual-v3",
            3,
            "0,5B, многоязычная, в звуке водяной знак",
            {"lang_code": "ru"},
            ("mlx-community/S3TokenizerV2",),
        ),
    )
}


# Коммит каждого репозитория на huggingface.co: веса — исполняемый код (pickle,
# custom code), без revision подмена файла в репозитории модели = исполнение
# кода (SEC-011). Обновить модель — поменять коммит здесь осознанно.
REVISIONS: dict[str, str] = {
    "bosonai/higgs-audio-v3-tts-4b": "239f63fb7b02b1aa085f98d9efae5e35cc5523e8",
    "mlx-community/fish-audio-s2-pro-bf16": "eccd57bf5c1ebc13cb2f993df867f4e49931a36a",
    "OpenMOSS-Team/MOSS-TTS-v1.5": "cdd3b911b1585e3f2dbc7775ef10f9926f58850a",
    "OpenMOSS-Team/MOSS-Audio-Tokenizer": "3cd226ba2947efa357ef453bcad111b6eafba782",
    "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16": "a6eb4f68e4b056f1215157bb696209bc82a6db48",
    "mlx-community/chatterbox-multilingual-v3": "03565773edd72e949572557597af8063bb49a18a",
    "mlx-community/S3TokenizerV2": "e0c9886f0e1c35ae85b1f27277416fb19fc72bec",
}


def cache_dir() -> Path:
    """Кэш HuggingFace этих моделей: <папка моделей>/mlx-audio."""
    return models.locate(CACHE_DIR, env="NARRATOR_MLX_CACHE")


def repo_dir(repo: str) -> Path:
    return cache_dir() / ("models--" + repo.replace("/", "--"))


def downloaded(engine: MlxEngine) -> bool:
    repos = (engine.repo, *engine.extra_repos)
    return all(any((repo_dir(repo) / "snapshots").glob("*/*")) for repo in repos)


def problem(engine_id: str) -> str | None:
    """Почему движок не запустится; None — всё на месте."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return "нужен Mac на Apple Silicon"
    if shutil.which("uv") is None:
        return "не найден uv"
    engine = MLX_ENGINES[engine_id]
    if not downloaded(engine):
        return f"модель не скачана: ./narrate setup --mlx {engine.id}"
    return None


def worker_command() -> list[str]:
    """Команда воркера: $NARRATOR_MLX_WORKER или uv с отдельным окружением mlx-audio."""
    custom = os.environ.get("NARRATOR_MLX_WORKER")
    if custom:
        import shlex

        return shlex.split(custom)
    return [
        "uv", "run", "--quiet", "--no-project", "--python", WORKER_PYTHON,
        "--with", MLX_AUDIO, "python", str(WORKER),
    ]  # fmt: skip


def worker_env() -> dict[str, str]:
    env = dict(os.environ)
    env["HF_HUB_CACHE"] = str(cache_dir())
    # Сеть не нужна и не трогается; NARRATOR_MLX_ONLINE=1 — разрешить докачку.
    if not env.get("NARRATOR_MLX_ONLINE"):
        env["HF_HUB_OFFLINE"] = "1"
    return env


class WorkerError(RuntimeError):
    pass


class Worker:
    """Процесс воркера с загруженной моделью: запросы и ответы — строки JSON."""

    def __init__(self, engine: MlxEngine, command: list[str] | None = None):
        self.engine = engine
        cmd = [*(command or worker_command()), "--model", engine.repo]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=worker_env(),
            )
        except OSError as e:
            raise WorkerError(f"воркер {engine.label} не запустился: {e}") from e
        self.sample_rate = int(self._read()["sample_rate"])

    def _read(self) -> dict:
        line = self._proc.stdout.readline()
        if not line:
            code = self._proc.wait()
            raise WorkerError(
                f"воркер {self.engine.label} завершился с кодом {code} — причина выше в выводе"
            )
        answer = json.loads(line)
        if "error" in answer:
            raise WorkerError(f"{self.engine.label}: {answer['error']}")
        return answer

    def synthesize(self, text: str, out: Path, ref_audio: Path, ref_text: str, kwargs: dict) -> int:
        """Озвучивает text в wav-файл out; возвращает число сэмплов."""
        request = {
            "text": text,
            "out": str(out),
            "ref_audio": str(ref_audio),
            "ref_text": ref_text,
            "kwargs": kwargs,
        }
        try:
            self._proc.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
        except OSError as e:
            raise WorkerError(f"воркер {self.engine.label} не отвечает: {e}") from e
        return int(self._read()["samples"])

    def close(self) -> None:
        proc, self._proc = getattr(self, "_proc", None), None
        if proc is None:
            return
        try:
            proc.stdin.close()  # конец ввода — воркер выходит сам и освобождает память
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            proc.wait()
        proc.stdout.close()

    def __del__(self):
        self.close()


def check_environment() -> str:
    """Собирает окружение воркера (первый раз качает mlx-audio); возвращает его версию."""
    out = subprocess.run(
        [*worker_command(), "--check"], capture_output=True, text=True, encoding="utf-8"
    )
    try:
        answer = json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as e:
        raise WorkerError(f"окружение mlx-audio не собралось:\n{out.stderr.strip()}") from e
    if "error" in answer:
        raise WorkerError(answer["error"])
    return answer["version"]


def download(engine: MlxEngine) -> Path:
    """Скачивает веса движка в кэш папки моделей; нужна сеть к huggingface.co."""
    from huggingface_hub import snapshot_download

    for repo in (engine.repo, *engine.extra_repos):
        snapshot_download(repo, revision=REVISIONS[repo], cache_dir=str(cache_dir()))
    return repo_dir(engine.repo)
