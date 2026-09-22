"""CSV -> Parquet normalization: the two probed format facts (ms-vs-us,
header-vs-no-header) and the monthly-over-daily partition rule."""

from __future__ import annotations

import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import polars as pl

from quantdesk import convert as c
from quantdesk.archive import InventoryRecord, append_inventory, sha256_of
from quantdesk.config import DATASETS

KLINE_1M = DATASETS["um_klines_1m"]
FUNDING = DATASETS["um_funding_rate"]


def _klines_csv(start_ms: int, n: int, unit: str = "ms") -> str:
    mult = 1000 if unit == "us" else 1
    step = 60_000
    rows = []
    for i in range(n):
        open_ms = (start_ms + i * step) * mult
        close_ms = open_ms + 59_999 * mult
        rows.append(f"{open_ms},1.0,2.0,0.5,1.5,10.0,{close_ms},15.0,5,5.0,7.5,0")
    return "\n".join(rows) + "\n"


def test_read_csv_ms_and_us_land_on_the_same_utc_grid() -> None:
    base = 1_717_200_000_000  # 2024-06-01T00:00 UTC in ms
    ms = c.read_csv(_klines_csv(base, 3).encode(), KLINE_1M)
    us = c.read_csv(_klines_csv(base, 3, unit="us").encode(), KLINE_1M)
    expected = datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert ms["open_time"].to_list() == us["open_time"].to_list()
    assert ms["open_time"][0] == expected
    assert ms.schema["open_time"] == pl.Datetime("us", "UTC")
    assert "ignore" not in ms.columns


def test_read_csv_funding_sniffs_its_header() -> None:
    csv = (
        "calc_time,funding_interval_hours,last_funding_rate\n"
        "1717200000000,8,0.0001\n1717228800000,8,-0.0002\n"
    )
    frame = c.read_csv(csv.encode(), FUNDING)
    assert frame["last_funding_rate"].to_list() == [0.0001, -0.0002]
    assert frame["calc_time"][0] == datetime(2024, 6, 1, tzinfo=timezone.utc)


def _inventory_record(key: str, rel: str, qdesk: Path, name: str, body: str):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as writer:
        writer.writestr(name, body)
    data = buffer.getvalue()
    target = qdesk / "raw" / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    record = InventoryRecord(
        key=key,
        path=rel,
        sha256=sha256_of(data),
        bytes=len(data),
        fetched_at="2026-09-22T00:00:00+00:00",
        remote_sha256=sha256_of(data),
    )
    append_inventory([record])
    return record


def test_convert_prefers_monthly_and_evicts_daily_parts(qdesk: Path) -> None:
    base = 1_717_200_000_000
    month_csv = _klines_csv(base, 60 * 24)  # one day worth is enough to exist
    day_csv = _klines_csv(base, 3)
    _inventory_record(
        "um_klines_1m/BTCUSDT/daily/2024-06-01",
        "data/futures/um/daily/klines/BTCUSDT/1m/BTCUSDT-1m-2024-06-01.zip",
        qdesk,
        "BTCUSDT-1m-2024-06-01.csv",
        day_csv,
    )
    result = c.convert_month(KLINE_1M, "BTCUSDT", "2024-06")
    assert result is not None and result.source == "daily:1"
    assert result.rows == 3

    _inventory_record(
        "um_klines_1m/BTCUSDT/monthly/2024-06",
        "data/futures/um/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-06.zip",
        qdesk,
        "BTCUSDT-1m-2024-06.csv",
        month_csv,
    )
    result = c.convert_month(KLINE_1M, "BTCUSDT", "2024-06")
    assert result is not None and result.source == "monthly"
    assert result.rows == 1440
    parts = list(
        (qdesk / "parquet" / "um_klines_1m" / "symbol=BTCUSDT").glob("*/*/*.parquet")
    )
    assert [p.name for p in parts] == ["part-2024-06.parquet"]  # stale gone
