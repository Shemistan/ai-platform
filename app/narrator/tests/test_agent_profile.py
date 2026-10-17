"""Профиль агентов в Makefile (SEC-004): claude -p только с узким набором прав."""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT_TARGETS = [
    "news-agent",
    "news-judge",
    "news-rescue",
    "news-weekly",
    "news-monthly",
    "article-prepare",
]


def dry_run(target: str) -> str:
    """Команда агента из `make -n`; CLAUDE=claude-stub — чтобы не спутать с другими строками."""
    out = subprocess.run(
        [
            "make",
            "-n",
            "-C",
            str(ROOT),
            target,
            "CLAUDE=claude-stub",
            "DAY=2026-10-16",
            "A=2026-10-16/x",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    lines = [ln for ln in out.splitlines() if ln.startswith("claude-stub")]
    assert len(lines) == 1, out
    return lines[0]


@pytest.mark.skipif(shutil.which("make") is None, reason="нет make")
@pytest.mark.parametrize("target", AGENT_TARGETS)
def test_agent_runs_with_narrow_profile(target):
    cmd = dry_run(target)
    assert cmd.startswith('claude-stub -p "')
    for flag in (
        "--setting-sources project",
        "--strict-mcp-config",
        "--tools ",
        "--disallowedTools",
    ):
        assert flag in cmd, flag
    for denied in (
        "Bash(gh *)",
        "Bash(curl *)",
        "Read(./bot_token.md)",
        "Bash(./narrate news publish *)",
    ):
        assert f'"{denied}"' in cmd, denied
    tools = cmd.split('--tools "', 1)[1].split('"', 1)[0].split(",")
    assert "Agent" not in tools and "NotebookEdit" not in tools


@pytest.mark.skipif(shutil.which("make") is None, reason="нет make")
def test_prompts_mark_external_text_as_data():
    for target in ("news-agent", "news-judge", "news-rescue", "article-prepare"):
        assert "только данные" in dry_run(target), target
