"""Общее для тестов narrator."""

import pytest


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    """Журнал комплаенса (CMP-015) — во временном каталоге, а не в настоящей библиотеке."""
    monkeypatch.setenv("NARRATOR_AUDIT", str(tmp_path / "audit.jsonl"))
