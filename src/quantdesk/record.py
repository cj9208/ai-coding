"""``quant record`` — the recorder whose only cost is uptime.

Three USD-M streams the monthly archives do **not** carry, so the one
way to ever backtest on them is to accumulate them from now on:

- ``liquidations``: the all-market force-order stream (``!forceOrder@arr``)
  — event-driven fast-band research (post-cascade reversion) is
  unverifiable without it, and there is no paid archive either;
- ``funding``: ``/fapi/v1/premiumIndex`` polled — last settled rate
  *plus* the estimated/premium mark per symbol, which the settled-only
  monthly funding archive throws away;
- ``open_interest``: ``/fapi/v1/openInterest`` polled per symbol on the
  5-minute grid — the exchange's own history endpoint only looks back
  ~30 days, so older OI exists nowhere but here.

Posture notes: writes are append-only flush parquets
(``recorded/<stream>/day=YYYY-MM-DD/part-<HHMMSS>.micros.parquet``) —
a crash loses at most one flush window and never rewrites history;
websocket dropouts reconnect with backoff and the gap is logged to
``recorded/gaps.log``, because a silent hole in a private dataset is
exactly the failure this whole layer exists to avoid. A connection that
stays up but pushes nothing is the same failure in disguise — on this
machine's network path ``fstream.binance.com`` answers pings yet delivers
zero data frames — so stream silence crosses the alert window and is
logged like any other gap.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import polars as pl
import websockets

from .config import paths

WS_BASE = "wss://fstream.binance.com/stream"
REST_BASE = "https://fapi.binance.com"

STREAMS = ("liquidations", "funding", "open_interest")


def parse_streams(spec: str) -> list[str]:
    names = [item.strip() for item in spec.split(",") if item.strip()]
    unknown = [name for name in names if name not in STREAMS]
    if unknown:
        raise SystemExit(
            f"unknown stream(s): {', '.join(unknown)}; available: {', '.join(STREAMS)}"
        )
    return names


_SCHEMAS: dict[str, dict[str, Any]] = {
    "liquidations": {
        "event_time": pl.Datetime("us", "UTC"),
        "symbol": pl.Utf8,
        "side": pl.Utf8,
        "price": pl.Float64,
        "quantity": pl.Float64,
    },
    "funding": {
        "seen_at": pl.Datetime("us", "UTC"),
        "symbol": pl.Utf8,
        "mark_price": pl.Float64,
        "index_price": pl.Float64,
        "estimated_settle_price": pl.Float64,
        "last_funding_rate": pl.Float64,
        "next_funding_time": pl.Datetime("us", "UTC"),
    },
    "open_interest": {
        "seen_at": pl.Datetime("us", "UTC"),
        "symbol": pl.Utf8,
        "open_interest": pl.Float64,
    },
}


def _utc_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def _ms_or_none(payload: dict, key: str) -> datetime | None:
    raw = payload.get(key)
    if raw in (None, "", 0):
        return None
    return _utc_ms(int(raw))


def parse_force_order(message: dict) -> dict | None:
    """One WS frame -> one liquidation row (None if not a forceOrder)."""
    data = message.get("data", message)
    if data.get("e") != "forceOrder":
        return None
    order = data["o"]
    return {
        "event_time": _utc_ms(int(data["E"])),
        "symbol": str(order["s"]),
        "side": str(order["S"]),  # side of the *liquidation* order
        "price": float(order["p"]),
        "quantity": float(order["q"]),
    }


def parse_premium_index(rows: list[dict], seen_at: datetime) -> list[dict]:
    return [
        {
            "seen_at": seen_at,
            "symbol": str(r["symbol"]),
            "mark_price": float(r["markPrice"]),
            "index_price": float(r["indexPrice"]),
            "estimated_settle_price": float(r.get("estimatedSettlePrice", "nan")),
            "last_funding_rate": float(r["lastFundingRate"]),
            "next_funding_time": _ms_or_none(r, "nextFundingTime"),
        }
        for r in rows
    ]


def parse_open_interest(symbol: str, payload: dict, seen_at: datetime) -> dict:
    return {
        "seen_at": seen_at,
        "symbol": symbol,
        "open_interest": float(payload["openInterest"]),
    }


class DayBucketWriter:
    """Buffers rows per (stream, day); each flush appends one parquet.

    Empty flushes write nothing — an idle hour must not litter the
    dataset with zero-row files that a continuity scan would flag.
    """

    def __init__(self) -> None:
        self.buffers: dict[str, list[dict]] = {}

    def add(self, stream: str, row: dict) -> None:
        day = row["event_time" if "event_time" in row else "seen_at"]
        key = f"{stream}/day={day:%Y-%m-%d}"
        self.buffers.setdefault(key, []).append(row)

    def flush(self) -> list[str]:
        written: list[str] = []
        for key, rows in self.buffers.items():
            if not rows:
                continue
            stream, day_part = key.split("/", 1)
            directory = paths().recorded / stream / day_part
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%H%M%S")
            out = directory / f"part-{stamp}.{time.monotonic_ns() % 1000000}.parquet"
            frame = pl.DataFrame(rows, schema=_SCHEMAS[stream])
            frame.write_parquet(out)
            written.append(f"{out.relative_to(paths().root)} rows={len(rows)}")
        self.buffers.clear()
        return written


def log_gap(what: str, detail: str) -> None:
    line = (
        f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} {what}: {detail}\n"
    )
    target = paths().recorded / "gaps.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(line)


@dataclass
class RecorderState:
    symbols: list[str]
    streams: list[str]
    minutes: float = 0.0  # 0 = run until interrupted
    funding_every: float = 60.0
    oi_every: float = 300.0
    flush_every: float = 30.0
    recv_timeout: float = 15.0
    silence_alert: float = 300.0
    writer: DayBucketWriter = field(default_factory=DayBucketWriter)
    until: float = 0.0

    def __post_init__(self) -> None:
        self.until = (time.monotonic() + self.minutes * 60) if self.minutes else 0.0


async def _ws_task(state: RecorderState) -> None:
    if "liquidations" not in state.streams:
        return
    url = f"{WS_BASE}?streams=!forceOrder@arr"
    backoff = 1.0
    while not state.until or time.monotonic() < state.until:
        try:
            async with websockets.connect(url, ping_interval=20) as ws:
                backoff = 1.0
                last_frame = time.monotonic()
                while not state.until or time.monotonic() < state.until:
                    try:
                        # not `async for ws`: a quiet stream must yield to a
                        # watchdog, and only a recv timeout gives one
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=state.recv_timeout
                        )
                    except TimeoutError:
                        if time.monotonic() - last_frame >= state.silence_alert:
                            log_gap(
                                "liquidations",
                                "connected but silent — no frames for "
                                f"{int(state.silence_alert)}s (pings answered;"
                                " the network path may drop push data)",
                            )
                            last_frame = time.monotonic()
                        continue
                    last_frame = time.monotonic()
                    row = parse_force_order(json.loads(raw))
                    if row:
                        state.writer.add("liquidations", row)
        except Exception as exc:  # noqa: BLE001 - any drop is a gap
            log_gap("liquidations", f"{type(exc).__name__}: {exc}; retry in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60.0)


async def _poll_task(state: RecorderState, path: str, stream: str, build) -> None:
    async with httpx.AsyncClient(base_url=REST_BASE, timeout=15.0) as client:
        while not state.until or time.monotonic() < state.until:
            started = time.monotonic()
            try:
                if stream == "open_interest":
                    for symbol in state.symbols:
                        response = await client.get(path, params={"symbol": symbol})
                        response.raise_for_status()
                        state.writer.add(
                            stream,
                            parse_open_interest(
                                symbol, response.json(), datetime.now(timezone.utc)
                            ),
                        )
                else:
                    response = await client.get(path)
                    response.raise_for_status()
                    rows = build(response.json(), datetime.now(timezone.utc))
                    for row in rows:
                        if not state.symbols or row["symbol"] in state.symbols:
                            state.writer.add(stream, row)
            except Exception as exc:  # noqa: BLE001 - a failed poll is a gap
                log_gap(stream, f"{type(exc).__name__}: {exc}")
            await asyncio.sleep(
                max(0.0, state.funding_every if stream == "funding" else state.oi_every)
                - (time.monotonic() - started)
            )


async def _flush_task(state: RecorderState) -> None:
    while not state.until or time.monotonic() < state.until:
        await asyncio.sleep(state.flush_every)
        for note in state.writer.flush():
            print(f"flush {note}", flush=True)


async def run(state: RecorderState) -> None:
    tasks = [asyncio.create_task(_flush_task(state))]
    if "liquidations" in state.streams:
        tasks.append(asyncio.create_task(_ws_task(state)))
    if "funding" in state.streams:
        tasks.append(
            asyncio.create_task(
                _poll_task(
                    state, "/fapi/v1/premiumIndex", "funding", parse_premium_index
                )
            )
        )
    if "open_interest" in state.streams and state.symbols:
        tasks.append(
            asyncio.create_task(
                _poll_task(state, "/fapi/v1/openInterest", "open_interest", None)
            )
        )
    # The tasks stop when their own loops notice the deadline; a *quiet*
    # websocket never wakes up to notice, so the wait is bounded here and
    # every task is cancelled on the way out.
    timeout = (state.until - time.monotonic()) if state.until else None
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), timeout)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for note in state.writer.flush():
            print(f"final flush {note}", flush=True)
