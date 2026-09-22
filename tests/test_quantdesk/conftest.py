"""Isolated data root per test — QUANTDESK_DATA_DIR is read at call
time (``config.paths()``), so one env var keeps every test off the real
``data/quantdesk/``."""

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def qdesk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path / "quantdesk"))
    yield tmp_path / "quantdesk"
