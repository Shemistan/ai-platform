"""Скачивание моделей закреплено (SEC-011): суммы, коммиты HF, версия huggingface_hub."""

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest

from narrator import mlx_engines, synth, verify

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "fetch-models.sh"


def script_var(name: str) -> str:
    found = re.search(rf"^{name}=(\S+)$", SCRIPT.read_text(encoding="utf-8"), re.M)
    assert found, name
    return found.group(1)


def test_script_and_code_agree():
    assert script_var("SILERO_SHA256") == synth.SILERO_SHA256
    assert script_var("REV_WHISPER") == verify.WHISPER_REVISION
    lock = (ROOT / "uv.lock").read_text(encoding="utf-8")
    hub = re.search(r'name = "huggingface-hub"\nversion = "([^"]+)"', lock).group(1)
    assert script_var("HF_HUB_VERSION") == hub


def test_every_hf_download_has_revision():
    calls = [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if re.match(r"\s*hf \S+/", line)
    ]
    assert calls
    assert all("--revision" in line for line in calls), calls
    repos = {r for e in mlx_engines.MLX_ENGINES.values() for r in (e.repo, *e.extra_repos)}
    assert repos == set(mlx_engines.REVISIONS)
    assert all(re.fullmatch(r"[0-9a-f]{40}", rev) for rev in mlx_engines.REVISIONS.values())


def test_snapshot_download_passes_revision(monkeypatch, tmp_path):
    import sys
    import types

    got = []
    fake = types.SimpleNamespace(
        snapshot_download=lambda repo, **kw: got.append((repo, kw["revision"]))
    )
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake)
    monkeypatch.setenv("NARRATOR_MLX_CACHE", str(tmp_path))
    mlx_engines.download(mlx_engines.MLX_ENGINES["moss"])
    assert got == [
        (r, mlx_engines.REVISIONS[r])
        for r in ("OpenMOSS-Team/MOSS-TTS-v1.5", "OpenMOSS-Team/MOSS-Audio-Tokenizer")
    ]


def test_silero_checksum_before_load(tmp_path, monkeypatch):
    model = tmp_path / "v5_5_ru.pt"
    model.write_bytes(b"model")
    with pytest.raises(synth.ModelMissingError, match="контрольная сумма"):
        synth.check_silero(model)
    monkeypatch.setattr(synth, "SILERO_SHA256", hashlib.sha256(b"model").hexdigest())
    synth.check_silero(model)


def test_fetch_script_rejects_tampered_silero(tmp_path):
    # curl «скачивает» файл с подменённым байтом — скрипт падает на сумме и не оставляет файл.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    curl = bin_dir / "curl"
    curl.write_text('#!/bin/sh\nwhile [ "$1" != "-o" ]; do shift; done\nprintf "tampered" > "$2"\n')
    curl.chmod(0o755)
    models = tmp_path / "models"
    models.mkdir()
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "NARRATOR_MODELS_DIR": str(models),
    }
    done = subprocess.run(["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "контрольная сумма" in done.stderr
    assert list((models / "silero").iterdir()) == []
