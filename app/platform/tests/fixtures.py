"""Общие данные тестов: статья, быстрые фейки, картинки, токен, дата (QA-017).

Тест-модули не импортируют друг друга: константы — отсюда, базовые классы — из helpers.
"""

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from aiplatform.fakes import fake_gen

ARTICLE = """Как устроен сборщик мусора в Go

Сборщик мусора в Go работает параллельно с программой. Он помечает живые объекты и освобождает остальные.

В новой версии паузы стали короче. Это важно для сервисов с жёсткими требованиями к задержкам.

Итог: обновляйтесь и измеряйте профили. Подписывайтесь на канал."""

# Фейки без пауз: шаг генерации и абзац озвучки — миллисекунды, речь вчетверо быстрее
# настоящей (короче аудио — короче рендер, QA-016).
FAST = {
    "FAKE_GEN_STEP_DELAY": "0.005",
    "FAKE_TTS_STEP_DELAY": "0.01",
    "FAKE_TTS_WORDS_PER_SECOND": "10",
    "FAKE_NEWS_DELAY": "0",
    "FAKE_LLM_FAIL": "",
    "FAKE_LLM_GARBAGE": "",
    "FAKE_NEWS_EMPTY": "",
}
RENDER_SCALE = "0.25"  # четверть от 1920×1080: рендер тестам нужен быстрым, не красивым
SOURCE = {"source_url": "https://example.dev/gc", "source_author": "Иван Петров"}
NEEDS_FFMPEG = unittest.skipUnless(shutil.which("ffmpeg"), "нужен ffmpeg")
TOKEN = "123456:" + "A" * 35  # по форме — токен бота, в сеть не уходит
# Фиксированная дата для тестов расписания, а не сегодняшняя: прогон через полночь её не сдвинет.
TODAY = date(2026, 3, 18)
PNG_STUB = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64  # сигнатура PNG без картинки — для приёма загрузок


def png(width: int = 64, height: int = 36, seed: int = 5) -> bytes:
    """Настоящая PNG заданного размера (fake_gen)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "p.png"
        fake_gen.write_png(str(path), width, height, seed)
        return path.read_bytes()


def png_bytes() -> bytes:
    return png(64, 36, 5)
