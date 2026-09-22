"""The tradable universe, snapshotted — because "today's list" is a claim
about one day.

Each ``quant universe sync`` appends a dated snapshot of the exchange
symbol lists (spot + USD-M futures, USDT quotes, TRADING status). One
snapshot is worthless for backtests; the *series* is what lets a later
as-of query say "pairs tradable on 2024-06-01" — the honest posture
against survivorship bias the plan demands (we accumulate; we do not
pretend to reconstruct what we never recorded).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import httpx

from .config import paths

#: market-data-only spot host (the api.binance.com alias is geo-blocked
#: in more places than the CloudFront-fronted data endpoints)
ENDPOINTS = {
    "spot": "https://data-api.binance.vision/api/v3/exchangeInfo",
    "um": "https://fapi.binance.com/fapi/v1/exchangeInfo",
}


@dataclass
class Snapshot:
    market: str
    day: str  # YYYY-MM-DD the snapshot describes
    symbols: list[str]


def _sync_url(market: str) -> str:
    return ENDPOINTS[market]


def fetch_snapshot(market: str, quote: str = "USDT") -> Snapshot:
    """Current TRADING symbols of one market, USDT-quoted by default."""
    response = httpx.get(_sync_url(market), timeout=30.0, follow_redirects=True)
    response.raise_for_status()
    rows = response.json().get("symbols", [])
    symbols = sorted(
        str(row["symbol"])
        for row in rows
        if str(row.get("status", "TRADING")).upper() in ("TRADING", "CLOSE_UP")
        and str(row["symbol"]).endswith(quote)
    )
    return Snapshot(market=market, day=date.today().isoformat(), symbols=symbols)


def store_snapshot(snapshot: Snapshot) -> Path:
    target = paths().universe / f"{snapshot.market}-{snapshot.day}.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(snapshot.__dict__, ensure_ascii=False, indent=0),
        encoding="utf-8",
    )
    return target


def latest(market: str) -> Snapshot | None:
    directory = paths().universe
    if not directory.is_dir():
        return None
    files = sorted(directory.glob(f"{market}-*.json"))
    if not files:
        return None
    return Snapshot(**json.loads(files[-1].read_text(encoding="utf-8")))


def synced_at() -> datetime | None:
    directory = paths().universe
    files = sorted(directory.glob("*.json")) if directory.is_dir() else []
    if not files:
        return None
    stamp = files[-1].name.rsplit("-", 1)[-1].removesuffix(".json")
    return datetime.strptime(stamp, "%Y-%m-%d").replace(tzinfo=timezone.utc)


#: 24h ticker stats, same hosts as ENDPOINTS.
TICKER_URLS = {
    "spot": "https://data-api.binance.vision/api/v3/ticker/24hr",
    "um": "https://fapi.binance.com/fapi/v1/ticker/24hr",
}


def rank_by_quote_volume(market: str, top: int) -> list[tuple[str, float]]:
    """Today's top-N USDT symbols by 24h quote volume.

    A *today* fact, so only TRADING symbols qualify (the latest snapshot
    filters what the ticker endpoint might still list) and whatever
    universe is frozen from it must carry this day's date — ranking
    history by today's activity is survivorship bias wearing a leaderboard.
    """
    snapshot = latest(market)
    if snapshot is None:
        raise SystemExit("no universe snapshot — run `quant universe sync`")
    trading = set(snapshot.symbols)
    rows = httpx.get(TICKER_URLS[market], timeout=30.0, follow_redirects=True).json()
    ranked = sorted(
        (
            (str(row["symbol"]), float(row["quoteVolume"]))
            for row in rows
            if str(row["symbol"]) in trading
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    return ranked[:top]
