"""Isolated data root per test — QUANTDESK_DATA_DIR is read at call
time (``config.paths()``), so one env var keeps every test off the real
``data/quantdesk/``."""

from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import polars as pl
import pytest


@pytest.fixture
def qdesk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setenv("QUANTDESK_DATA_DIR", str(tmp_path / "quantdesk"))
    yield tmp_path / "quantdesk"


@pytest.fixture
def synth() -> SimpleNamespace:
    """Deterministic synthetic daily closes for the factor/screen tests."""
    start = date(2022, 1, 1)

    def make_wide(days: int, ramps: dict[str, float]) -> pl.DataFrame:
        """One symbol per entry; close(t) = 100 * (1 + ramp)^t."""
        rows: list[dict[str, object]] = []
        for i in range(days):
            row: dict[str, object] = {"date": start + timedelta(days=i)}
            for symbol, ramp in ramps.items():
                row[symbol] = 100.0 * (1.0 + ramp) ** i
            rows.append(row)
        return pl.DataFrame(rows)

    return SimpleNamespace(START=start, make_wide=make_wide)
