"""tools/setup-model.sh: ставит модели фото и видео без соседних репозиториев."""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        for model in (*photo_models.CATALOG, *video_models.CATALOG):
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
        for model in (*photo_models.CATALOG, *video_models.CATALOG):
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
        for name in ("MFLUX_VERSION=", "FLUX2_VERSION=", "LTX_VERSION=", "WAN_REVISION="):
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


if __name__ == "__main__":
    unittest.main()
