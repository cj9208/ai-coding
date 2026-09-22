"""Recorder parsing and the append-only day-bucket flush (offline)."""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import polars as pl
import pytest

from quantdesk import record

FORCE_ORDER = {
    "stream": "!forceOrder@arr",
    "data": {
        "e": "forceOrder",
        "E": 1717200000123,
        "o": {"s": "BTCUSDT", "S": "SELL", "q": "0.140", "p": "66000.10"},
    },
}

PREMIUM = [
    {
        "symbol": "BTCUSDT",
        "markPrice": "67500.5",
        "indexPrice": "67510.2",
        "estimatedSettlePrice": "67505.0",
        "lastFundingRate": "0.00010000",
        "nextFundingTime": 1717228800000,
        "time": 1717200000000,
    }
]


def test_parse_force_order_row() -> None:
    row = record.parse_force_order(FORCE_ORDER)
    assert row is not None
    assert row["symbol"] == "BTCUSDT"
    assert row["side"] == "SELL"
    assert row["price"] == 66000.10 and row["quantity"] == 0.14
    assert row["event_time"] == datetime.fromtimestamp(1717200000.123, tz=timezone.utc)


def test_parse_ignores_non_liquidation_frames() -> None:
    assert record.parse_force_order({"data": {"e": "kline"}}) is None


def test_parse_premium_index_maps_funding_fields() -> None:
    seen = datetime(2024, 6, 1, tzinfo=timezone.utc)
    (row,) = record.parse_premium_index(PREMIUM, seen)
    assert row["last_funding_rate"] == 0.0001
    assert row["estimated_settle_price"] == 67505.0
    assert row["next_funding_time"] == datetime(2024, 6, 1, 8, tzinfo=timezone.utc)


def test_day_bucket_flush_writes_typed_parquet(qdesk: Path) -> None:
    writer = record.DayBucketWriter()
    row = record.parse_force_order(FORCE_ORDER)
    assert row is not None
    writer.add("liquidations", row)
    writer.add("liquidations", row)
    notes = writer.flush()
    assert len(notes) == 1 and "rows=2" in notes[0]
    files = list((qdesk / "recorded" / "liquidations").rglob("*.parquet"))
    assert len(files) == 1
    frame = pl.read_parquet(files[0])
    assert frame.height == 2
    assert frame.schema["event_time"] == pl.Datetime("us", "UTC")
    assert writer.flush() == []  # empty buffer writes nothing


async def test_run_exits_at_deadline_and_final_flushes(qdesk: Path) -> None:
    """The live-smoke regression: a flush task sleeping past the deadline
    must not keep ``run()`` alive — the wait itself is bounded."""
    row = record.parse_force_order(FORCE_ORDER)
    assert row is not None
    state = record.RecorderState(symbols=[], streams=[], minutes=0.005)  # 0.3s
    state.writer.add("liquidations", row)
    await record.run(state)
    files = list((qdesk / "recorded" / "liquidations").rglob("*.parquet"))
    assert len(files) == 1
    assert pl.read_parquet(files[0]).height == 1


class _SilentWs:
    """An open connection that never yields a frame — what fstream's data
    path looks like on a network that answers pings but drops pushes."""

    async def __aenter__(self) -> "_SilentWs":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def recv(self) -> str:
        await asyncio.sleep(0.05)
        raise TimeoutError


async def test_silent_stream_is_logged_as_a_gap(
    qdesk: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(record.websockets, "connect", lambda url, **kw: _SilentWs())
    state = record.RecorderState(
        symbols=[], streams=["liquidations"], recv_timeout=0.1, silence_alert=0.2
    )
    state.until = time.monotonic() + 0.5
    await asyncio.wait_for(record._ws_task(state), 5)
    gaps = (qdesk / "recorded" / "gaps.log").read_text(encoding="utf-8")
    assert "connected but silent" in gaps
