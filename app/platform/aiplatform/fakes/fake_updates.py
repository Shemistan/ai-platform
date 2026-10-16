"""Имитация источников обновлений для демо и тестов: PyPI, GitHub и Hugging Face без сети.

Версии подобраны так, чтобы на вкладке были все состояния: mflux и ltx-2-mlx
устарели, Flux2CLI свежий, часть весов обновлена после установки.
"""

from __future__ import annotations

INSTALLED = {
    "mflux": "0.22.0",
    "flux2cli": "3.1.0",
    "ltx-2-mlx": "0.16.2",
    "wan": "86bfc98",
    "installed_at": "2026-06-01",
}

RESPONSES = {
    "https://pypi.org/pypi/mflux/json": {"info": {"version": "0.23.1"}},
    "https://api.github.com/repos/VincentGourbin/flux-2-swift-mlx/releases/latest": {"tag_name": "v3.1.0"},
    "https://api.github.com/repos/dgrauet/ltx-2-mlx/tags?per_page=50": [
        {"name": "v0.17.0"},
        {"name": "v0.16.2"},
        {"name": "v0.9.1"},
    ],
    "https://huggingface.co/api/models/mflux-community/z-image-turbo-mflux-q8": {
        "sha": "f00dfeed00",
        "lastModified": "2026-09-20T10:00:00.000Z",
    },
    "https://huggingface.co/api/models/PocketAiHub/Wan2.2-T2V-A14B-MLX": {
        "sha": "86bfc9847512a9eb66886388b8b92ba81e0b6729",
        "lastModified": "2026-05-01T10:00:00.000Z",
    },
}
OLD_WEIGHTS = {"sha": "0ld0ld0ld0", "lastModified": "2026-03-01T10:00:00.000Z"}


def fetch(url: str) -> object:
    if url in RESPONSES:
        return RESPONSES[url]
    if url.startswith("https://huggingface.co/api/models/"):
        return OLD_WEIGHTS
    raise OSError(f"демо: нет ответа для {url}")
