"""Fixture: every test gets its own ledger file via NOTIFY_DATA_DIR."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def ledger_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "notify"
    monkeypatch.setenv("NOTIFY_DATA_DIR", str(d))
    monkeypatch.delenv("NOTIFY_CHANNELS", raising=False)
    return d
