"""Общее для тестов narrator."""

import pytest


@pytest.fixture(autouse=True)
def isolated_audit(tmp_path, monkeypatch):
    """Журнал комплаенса (CMP-015) — во временном каталоге, а не в настоящей библиотеке."""
    monkeypatch.setenv("NARRATOR_AUDIT", str(tmp_path / "audit.jsonl"))


@pytest.fixture(autouse=True)
def isolated_platform_registry(tmp_path, monkeypatch):
    """Реестр платформы (CMP-022) — пустой временный каталог, а не настоящий с этой машины."""
    monkeypatch.setenv("AIP_DATA_DIR", str(tmp_path / "platform-data"))
