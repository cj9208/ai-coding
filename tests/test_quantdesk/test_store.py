"""Integrity checks: tampered bytes, grid gaps, and the universe
snapshot round-trip (offline)."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from quantdesk import store, universe
from quantdesk.archive import InventoryRecord, append_inventory, sha256_of
from quantdesk.config import DATASETS

KLINE_1M = DATASETS["um_klines_1m"]
BASE_US = 1_717_200_000_000_000  # 2024-06-01T00:00Z, microseconds


def _grid_frame(rows: int, skip: set[int] | None = None) -> pl.DataFrame:
    idx = [i for i in range(rows) if not skip or i not in skip]
    times = (
        (pl.Series(idx, dtype=pl.Int64) * 60_000_000 + BASE_US)
        .cast(pl.Datetime("us"))
        .dt.replace_time_zone("UTC")
    )
    return pl.DataFrame({"open_time": times, "close": [1.0] * len(idx)})


def _write_month(symbol: str, month: str, frame: pl.DataFrame, qdesk: Path) -> Path:
    target = (
        qdesk
        / "parquet"
        / KLINE_1M.dir_slug
        / f"symbol={symbol}"
        / "year=2024"
        / "month=06"
    )
    target.mkdir(parents=True, exist_ok=True)
    out = target / f"part-{month}.parquet"
    frame.write_parquet(out)
    return out


def test_grid_check_flags_missing_bars(qdesk: Path) -> None:
    full = _grid_frame(1440)
    _write_month("BTCUSDT", "2024-06", full, qdesk)
    assert store.check_grid(KLINE_1M, "BTCUSDT", ["2024-06"]) == []
    _write_month("BTCUSDT", "2024-06", _grid_frame(1440, {100, 200, 300}), qdesk)
    issues = store.check_grid(KLINE_1M, "BTCUSDT", ["2024-06"])
    assert len(issues) == 1 and issues[0].kind == "grid"


def test_local_hash_detects_tampering(qdesk: Path) -> None:
    rel = "data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-06.zip"
    raw = qdesk / "raw" / rel
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_bytes(b"zipbytes")
    append_inventory(
        [
            InventoryRecord(
                key="um_klines_1m/BTCUSDT/monthly/2024-06",
                path=rel,
                sha256=sha256_of(b"different"),
                bytes=8,
                fetched_at="2026-09-22T00:00:00+00:00",
            )
        ]
    )
    issues = store.check_local_hashes()
    assert [i.kind for i in issues] == ["local-hash"]


def test_universe_snapshot_roundtrip(qdesk: Path) -> None:
    snap = universe.Snapshot(market="um", day="2026-09-22", symbols=["BTCUSDT"])
    universe.store_snapshot(snap)
    latest = universe.latest("um")
    assert latest == snap
