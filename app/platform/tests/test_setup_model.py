"""tools/setup-model.sh: ставит модели фото и видео без соседних репозиториев."""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from aiplatform.avatar import models as avatar_models
from aiplatform.photo import models as photo_models
from aiplatform.updates import checks
from aiplatform.video import models as video_models

SERVICE_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SERVICE_DIR / "tools" / "setup-model.sh"
README = SERVICE_DIR / "README.md"


def run(*args: str, cwd: Path = SERVICE_DIR, env: dict | None = None) -> subprocess.CompletedProcess:
    full = {**os.environ, "PYTHON": sys.executable, "PYTHONPATH": str(SERVICE_DIR), **(env or {})}
    return subprocess.run(
        ["sh", str(cwd / "tools" / "setup-model.sh"), *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=full,
    )


class CatalogTest(unittest.TestCase):
    def test_lists_every_catalog_model(self):
        proc = run("--list")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        listed = proc.stdout.replace(";", " ").split()
        for model in (*photo_models.CATALOG, *video_models.CATALOG, *avatar_models.CATALOG):
            with self.subTest(model=model.id):
                self.assertIn(model.id, listed)

    def test_unknown_model_fails(self):
        proc = run("no-such-model")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("неизвестная модель «no-such-model»", proc.stderr)
        self.assertIn("z-image-turbo", proc.stderr)

    def test_readme_gives_size_of_every_model(self):
        text = README.read_text(encoding="utf-8")
        section = text[text.index("### Установка моделей") :]
        section = section[: section.index("\n### ", 1)]
        for model in (*photo_models.CATALOG, *video_models.CATALOG, *avatar_models.CATALOG):
            row = next((line for line in section.splitlines() if f"`{model.id}`" in line), "")
            with self.subTest(model=model.id):
                self.assertIn("ГБ", row, f"нет строки с размером весов для {model.id}")

    def test_hints_point_to_script_in_this_repo(self):
        from aiplatform.photo import jobs as photo_jobs
        from aiplatform.video import jobs as video_jobs

        for hint in (photo_jobs.INSTALL_HINT, video_jobs.INSTALL_HINT, checks.PHOTOGEN_SETUP, checks.VIDEOGEN_SETUP):
            with self.subTest(hint=hint):
                self.assertNotIn("_generator", hint)
        self.assertTrue((SERVICE_DIR.parent.parent / checks.PHOTOGEN_SETUP).is_file())
        self.assertTrue((SERVICE_DIR.parent.parent / checks.VIDEOGEN_SETUP).is_file())

    def test_script_keeps_versions_the_updates_tab_talks_about(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for name in ("MFLUX_VERSION=", "FLUX2_VERSION=", "LTX_VERSION=", "WAN_REVISION=", "SADTALKER_REVISION="):
            self.assertIn(name, text)


@unittest.skipUnless(platform.system() == "Darwin" and platform.machine() == "arm64", "только Mac на Apple Silicon")
@unittest.skipUnless(shutil.which("uv"), "нет uv")
class SettingsTest(unittest.TestCase):
    """Уже поставленная модель не качается, а local.mk дописывается, не теряя своих строк."""

    def test_installed_model_updates_local_mk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            shutil.copy(SCRIPT, root / "tools" / SCRIPT.name)
            models_dir = root / "models"
            model = next(m for m in photo_models.CATALOG if m.id == "z-image-turbo")
            (models_dir / model.weights).mkdir(parents=True)
            (models_dir / model.weights / photo_models.MARKER).touch()
            photo_home = root / "photogen"
            mflux_bin = photo_home / "mflux-0.22.0" / "bin"
            mflux_bin.mkdir(parents=True)
            (mflux_bin / "mflux-generate-qwen").write_text("#!/bin/sh\n")
            (mflux_bin / "mflux-generate-qwen").chmod(0o755)
            (root / "local.mk").write_text("AIP_LLM_CMD=claude -p\nAIP_MODELS_DIR=/старый/путь\n")

            proc = run(
                "z-image-turbo",
                cwd=root,
                env={
                    "AIP_MODELS_DIR": str(models_dir),
                    "PHOTOGEN_HOME": str(photo_home),
                    "VIDEOGEN_HOME": str(root / "videogen"),
                },
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("z-image-turbo уже установлена", proc.stdout)
            lines = (root / "local.mk").read_text().splitlines()
            self.assertIn("AIP_LLM_CMD=claude -p", lines)
            self.assertIn(f"AIP_MODELS_DIR={models_dir}", lines)
            self.assertNotIn("AIP_MODELS_DIR=/старый/путь", lines)
            self.assertIn(f"AIP_PHOTO_MFLUX_BIN={mflux_bin}", lines)
            self.assertFalse(any(line.startswith("AIP_VIDEO_") for line in lines))


def fake_tree(root: Path) -> None:
    """Каталог сервиса для прогона копии скрипта: tools/setup-model.sh."""
    (root / "tools").mkdir()
    shutil.copy(SCRIPT, root / "tools" / SCRIPT.name)


class MoveToTest(unittest.TestCase):
    """--move-to переносит веса целиком и правит AIP_MODELS_DIR в local.mk (без сети и uv)."""

    def test_moves_weights_and_updates_local_mk(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_tree(root)
            old, new = root / "old", root / "new disk"
            (old / "z-image-turbo-mflux-q8").mkdir(parents=True)
            (old / "z-image-turbo-mflux-q8" / "w.safetensors").write_text("веса")
            (old / ".photogen-cache" / "hf").mkdir(parents=True)
            (root / "local.mk").write_text(f"AIP_LLM_CMD=claude -p\nAIP_MODELS_DIR={old}\n")

            proc = run("--move-to", str(new), cwd=root, env={"AIP_MODELS_DIR": ""})  # каталог — из local.mk

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((new / "z-image-turbo-mflux-q8" / "w.safetensors").read_text(), "веса")
            self.assertTrue((new / ".photogen-cache" / "hf").is_dir())
            self.assertFalse(old.exists())
            lines = (root / "local.mk").read_text().splitlines()
            self.assertIn("AIP_LLM_CMD=claude -p", lines)
            self.assertIn(f"AIP_MODELS_DIR={new}", lines)
            self.assertNotIn(f"AIP_MODELS_DIR={old}", lines)

    def test_clash_moves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_tree(root)
            old, new = root / "old", root / "new"
            (old / "krea").mkdir(parents=True)
            (old / "zimage").mkdir()
            (new / "krea").mkdir(parents=True)

            proc = run("--move-to", str(new), cwd=root, env={"AIP_MODELS_DIR": str(old)})

            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("уже есть: krea", proc.stderr)
            self.assertTrue((old / "zimage").is_dir())
            self.assertFalse((root / "local.mk").exists())

    def test_makefile_target_needs_destination(self):
        proc = subprocess.run(["make", "-s", "-C", str(SERVICE_DIR), "move-models", f"PYTHON={sys.executable}"],
                              capture_output=True, text=True, timeout=60)  # fmt: skip
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("make move-models TO=", proc.stderr)


class EncoderCatalogTest(unittest.TestCase):
    def test_klein_encoder_matches_flux2_text_quant(self):
        klein = next(m for m in photo_models.CATALOG if m.id == "flux2-klein-9b")
        self.assertEqual(photo_models.text_encoder(klein), f"lmstudio-community/Qwen3-8B-MLX-{klein.text_quant}")
        mflux = next(m for m in photo_models.CATALOG if m.backend == photo_models.MFLUX)
        self.assertEqual(photo_models.text_encoder(mflux), "")
        for model in photo_models.CATALOG:
            with self.subTest(model=model.id):
                self.assertEqual(len(photo_models.setup_line(model).split()), 7)


@unittest.skipUnless(platform.system() == "Darwin" and platform.machine() == "arm64", "только Mac на Apple Silicon")
@unittest.skipUnless(shutil.which("uv"), "нет uv")
class Flux2ExtrasTest(unittest.TestCase):
    """У поставленной FLUX.2 Klein докладываются mlx.metallib и текстовый энкодер (на фейках)."""

    def test_metallib_and_encoder_for_installed_klein(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fake_tree(root)
            models_dir = root / "models"
            flux_dir = models_dir / photo_models.FLUX2_DIR
            flux_dir.mkdir(parents=True)
            (flux_dir / f"{photo_models.MARKER}-klein-9b").touch()
            photo_home = root / "photogen"
            flux_bin = photo_home / "flux2cli-v3.1.0" / "Flux2CLI"
            flux_bin.parent.mkdir(parents=True)
            flux_bin.write_text("#!/bin/sh\n")
            flux_bin.chmod(0o755)
            # mflux «стоит»: его hf пишет, с чем его позвали, и кладёт веса как настоящий.
            mflux_bin = photo_home / "mflux-0.22.0" / "bin"
            mflux_bin.mkdir(parents=True)
            (mflux_bin / "mflux-generate-qwen").write_text("#!/bin/sh\n")
            (mflux_bin / "mflux-generate-qwen").chmod(0o755)
            calls = root / "hf-calls"
            (mflux_bin / "hf").write_text(
                f'#!/bin/sh\necho "$HF_HOME $*" >> "{calls}"\nmkdir -p "$4" && touch "$4/model.safetensors"\n'
            )
            (mflux_bin / "hf").chmod(0o755)
            metallib = root / "mlx.metallib"
            metallib.write_bytes(b"metal")
            env = {
                "AIP_MODELS_DIR": str(models_dir),
                "PHOTOGEN_HOME": str(photo_home),
                "VIDEOGEN_HOME": str(root / "videogen"),
                "MLX_METALLIB": str(metallib),
            }

            proc = run("flux2-klein-9b", cwd=root, env=env)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual((flux_bin.parent / "mlx.metallib").read_bytes(), b"metal")
            hf_home = models_dir / photo_models.CACHE_DIR / "hf"
            repo = "lmstudio-community/Qwen3-8B-MLX-4bit"
            expected = [str(hf_home), "download", repo, "--local-dir", str(models_dir / "models" / repo)]
            self.assertEqual(calls.read_text().split(), expected)
            self.assertIn(f'AIP_PHOTO_FLUX2_CMD="{flux_bin}"', (root / "local.mk").read_text())

            # Второй прогон: всё на месте — ни копирования, ни скачивания.
            proc = run("flux2-klein-9b", cwd=root, env={**env, "MLX_METALLIB": str(root / "нет-файла")})
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(len(calls.read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
