"""Команда для `pocketai-wan-video` (Wan 2.2 T2V A14B) и разбор его вывода в прогресс.

Формат флагов и вывода сверен с исходниками запускателя из репозитория весов
PocketAiHub/Wan2.2-T2V-A14B-MLX (mlx-video, wan_2/generate.py): этапы печатаются
строками `Loading T5 encoder...`, шаги — полосой tqdm `Diffusion:  30%|███ | 3/10 [...]`.
"""

from __future__ import annotations

from pathlib import Path

from aiplatform.video.ltx import _TQDM
from aiplatform.video.timeline import GenerationSpec

# Доля общего прогресса: до шумоподавления, само шумоподавление, декодирование.
_DENOISE = (10.0, 88.0)
_DECODE = 90.0
_FINISHING_CAP = 99.0

_STAGES = (
    ("Loading T5 encoder", "Загрузка текстового энкодера", 2.0),
    ("Encoding text", "Кодирование промпта", 5.0),
    ("Loading transformer", "Загрузка модели", 8.0),
    ("Denoising", "Шумоподавление", _DENOISE[0]),
    ("Decoding with VAE", "Декодирование видео", _DECODE),
)


def build_argv(cmd: tuple[str, ...], spec: GenerationSpec, output: Path, *, model: str, steps: int) -> list[str]:
    """argv для генерации по промпту. Guidance и shift пайплайн берёт из config.json модели."""
    if spec.keyframes:
        raise ValueError("Wan T2V не принимает кадры")
    return [
        *cmd,
        "--model-dir", model,
        f"--prompt={spec.prompt}",  # слитно: промпт может начинаться с дефиса
        "--output-path", str(output),
        "--width", str(spec.width),
        "--height", str(spec.height),
        "--num-frames", str(spec.num_frames),
        "--steps", str(steps),
        "--scheduler", "unipc",
        "--seed", str(spec.seed),
    ]  # fmt: skip


class ProgressParser:
    """Копит состояние прогресса по строкам вывода процесса."""

    def __init__(self) -> None:
        self.stage = "Запуск"
        self.step: int | None = None
        self.steps: int | None = None
        self.percent = 0.0

    def snapshot(self) -> dict:
        return {
            "stage": self.stage,
            "step": self.step,
            "steps": self.steps,
            "percent": round(self.percent, 1),
        }

    def _raise_percent(self, value: float) -> None:
        self.percent = max(self.percent, min(value, _FINISHING_CAP))

    def feed(self, line: str) -> bool:
        """Учесть строку. True — если это строка прогресса (в лог её не пишем)."""
        line = line.strip()
        if not line:
            return False
        for prefix, text, percent in _STAGES:
            if line.startswith(prefix):
                self.stage = text
                self.step = self.steps = None
                self._raise_percent(percent)
                return False

        bar = _TQDM.match(line)
        if not bar:
            return False
        desc = bar["desc"].strip()
        n, total = float(bar["n"]), float(bar["total"])
        if desc.startswith("Diffusion"):
            self.step, self.steps = int(n), int(total)
            self.stage = "Шумоподавление"
            lo, hi = _DENOISE
            self._raise_percent(lo + (hi - lo) * (n / total if total else 0.0))
        else:
            self.stage = f"{desc or 'Загрузка'} — {bar['pct']}%"
            self.step = self.steps = None
        return True
