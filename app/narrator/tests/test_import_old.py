"""tools/import-old.sh: данные старого narrator, токен — строкой в .env корня репозитория."""

import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "import-old.sh"
FAKE_TOKEN = "123456:" + "A" * 35  # синтетика: настоящий токен в тестах держать нельзя


def run(tmp_path: Path) -> tuple[subprocess.CompletedProcess, Path]:
    """Копия скрипта в каркасе <repo>/app/narrator/tools, старый каталог с токеном и каналами."""
    tools = tmp_path / "repo" / "app" / "narrator" / "tools"
    tools.mkdir(parents=True)
    shutil.copy(SCRIPT, tools / SCRIPT.name)
    old = tmp_path / "old"
    (old / "library").mkdir(parents=True)
    (old / "bot_token.md").write_text(f"# токен\n\n`{FAKE_TOKEN}`\n")
    (old / "telegram_tems.md").write_text("VOICE_NEWS=-1001234567890\n")
    done = subprocess.run(
        ["sh", str(tools / SCRIPT.name), str(old)], capture_output=True, text=True, check=True
    )
    return done, tmp_path / "repo" / ".env"


@pytest.mark.skipif(shutil.which("rsync") is None, reason="нет rsync")
def test_token_goes_to_env_file(tmp_path):
    done, env = run(tmp_path)
    assert env.read_text() == f"NARRATOR_TELEGRAM_TOKEN={FAKE_TOKEN}\n"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    assert not (env.parent / "app" / "narrator" / "bot_token.md").exists()
    assert (env.parent / "app" / "narrator" / "telegram_tems.md").exists()
    assert FAKE_TOKEN not in done.stdout + done.stderr


@pytest.mark.skipif(shutil.which("rsync") is None, reason="нет rsync")
def test_existing_token_is_kept(tmp_path):
    env = tmp_path / "repo" / ".env"
    env.parent.mkdir()
    env.write_text("export NARRATOR_TELEGRAM_TOKEN=свой\n")
    done, _ = run(tmp_path)
    assert env.read_text() == "export NARRATOR_TELEGRAM_TOKEN=свой\n"
    assert "уже есть" in done.stdout
