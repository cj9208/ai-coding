"""Paths and the dataset registry — the two facts every other module needs.

Data lives under ``data/quantdesk/`` (repo-root anchored per the house
rule; ``QUANTDESK_DATA_DIR`` overrides) split into ``raw/`` — the
downloaded archives, byte-identical to upstream plus an ``inventory
.jsonl`` of what their sha256 was when fetched — and ``parquet/``, the
normalized hive layout that is the source of truth for research.

The registry mirrors ``ocr_backend.models.MODELS``: adding a dataset is
one entry, and URL shape/file naming for every archive derives from it
in exactly one place (:func:`archive.relative_path`).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import NamedTuple

from utils.paths import data_dir


class Paths(NamedTuple):
    root: Path
    raw: Path
    parquet: Path
    inventory: Path
    universe: Path
    recorded: Path


def paths() -> Paths:
    """Resolve the data directories now (tests repoint the env var)."""
    env = os.environ.get("QUANTDESK_DATA_DIR")
    root = Path(env) if env else data_dir("quantdesk")
    return Paths(
        root=root,
        raw=root / "raw",
        parquet=root / "parquet",
        inventory=root / "inventory.jsonl",
        universe=root / "universe",
        recorded=root / "recorded",
    )


class Dataset(NamedTuple):
    """One downloadable family inside Binance's public archive.

    ``step_us`` is the expected row spacing in microseconds for the
    time-indexed datasets (0 for event streams like aggTrades where
    rows are not on a grid); continuity checks assert
    ``(max - min) // step + 1 == rows``.
    """

    name: str
    market: str  # URL segment: "spot" | "futures/um"
    kind: str  # URL segment: "klines" | "aggTrades" | "fundingRate"
    interval: str  # klines interval; "" when the kind has no interval
    monthly: bool
    daily: bool
    time_col: str
    step_us: int

    @property
    def dir_slug(self) -> str:
        """Directory name under ``parquet/`` (hive-safe, no slashes)."""
        return self.name.replace("/", "_")


_SPOT_KLINE_STEP = {"1m": 60_000_000, "1s": 1_000_000, "1d": 86_400_000_000}


def _klines(market: str, interval: str, name: str) -> Dataset:
    return Dataset(
        name=name,
        market=market,
        kind="klines",
        interval=interval,
        monthly=True,
        daily=True,
        time_col="open_time",
        step_us=_SPOT_KLINE_STEP[interval],
    )


#: registry: a new dataset is one entry here (and its columns in
#: ``convert.CSV_SCHEMAS`` if not already covered).
DATASETS: dict[str, Dataset] = {
    d.name: d
    for d in [
        _klines("spot", "1m", "spot_klines_1m"),
        _klines("spot", "1s", "spot_klines_1s"),
        _klines("spot", "1d", "spot_klines_1d"),
        _klines("futures/um", "1m", "um_klines_1m"),
        _klines("futures/um", "1d", "um_klines_1d"),
        Dataset(
            name="um_funding_rate",
            market="futures/um",
            kind="fundingRate",
            interval="",
            monthly=True,
            daily=False,  # funding archives are published monthly only
            time_col="calc_time",
            # no fixed step: the exchange compresses settlement intervals
            # (8h -> 4h -> 2h) and sometimes skips a single settlement, so
            # continuity is the interval-aware store.check_funding_grid
            step_us=0,
        ),
        Dataset(
            name="spot_agg_trades",
            market="spot",
            kind="aggTrades",
            interval="",
            monthly=True,
            daily=True,
            time_col="transact_time",
            step_us=0,
        ),
    ]
}


def parse_csv_list(spec: str) -> list[str]:
    """``"a,b ,c"`` -> ``["a", "b", "c"]`` for CLI list arguments."""
    return [item.strip() for item in spec.split(",") if item.strip()]
