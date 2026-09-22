"""The factor layer — pure functions, structurally future-blind.

One convention, enforced at the input boundary instead of by discipline:
every factor takes the full price/funding history **and an `as_of`
date**, and its first statement filters to `date <= as_of`. A factor
physically cannot see tomorrow's bar, so the classic quant failure —
training on data the signal could not have had — is unreachable code,
not an attestation. The shift convention (signal at bar t close, fill
at t+1) then lives in the screening engine (:mod:`quantdesk.screen`),
which is the only place time moves forward.

Functions here return *target positions* as of `as_of`; the slow band
is long-only (the plan's cost posture: daily-turnover long-short cannot
pay 10 bp/side), the fast band gross-1 long-short.

The factors themselves are the plan's ranked candidates: cross-sectional
momentum in the "avoid losers" form, time-series momentum with vol
targeting (the benchmark every other idea must beat), and extreme
funding percentile reversal (the fast-band idea whose history is free).
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Callable

import polars as pl

from .config import DATASETS, paths

DATE_COL = "date"
MIN_OBS_DEFAULT = 250  # a symbol needs ~1y of bars before it can score


def _symbol_of(partition_file: Path) -> str:
    """A hive leaf lives under symbol=…/year=…/month=…, so its symbol
    is an ancestor directory, not the immediate parent — walk the path."""
    for part in reversed(partition_file.parts):
        if part.startswith("symbol="):
            return part.removeprefix("symbol=")
    raise ValueError(f"no symbol= partition in {partition_file}")


def load_closes(dataset: str, symbols: list[str]) -> pl.DataFrame:
    """Wide daily closes: one `date` column plus one Float64 per symbol.

    Reads straight off the hive partitions — filesystem-is-the-index,
    same posture as ``store.scan_coverage``. Missing listing history
    stays NaN rather than being back-filled: a factor must not be able
    to score a symbol into existence before it traded.
    """
    slug = DATASETS[dataset].dir_slug
    root = paths().parquet / slug
    frames = []
    for path in sorted(root.rglob("part-*.parquet")):
        sym = _symbol_of(path)
        if sym in set(symbols):
            frame = pl.read_parquet(path)
            frames.append(
                frame.select(
                    pl.col("open_time").dt.date().alias(DATE_COL),
                    pl.lit(sym).alias("symbol"),
                    pl.col("close"),
                )
            )
    if not frames:
        raise SystemExit(f"no hive partitions for {dataset} — run quant convert")
    wide = (
        pl.concat(frames)
        .unique(subset=[DATE_COL, "symbol"])
        .pivot(on="symbol", index=DATE_COL, values="close")
        .sort(DATE_COL)
    )
    return wide


def load_funding(symbols: list[str]) -> pl.DataFrame:
    """Long funding history: (symbol, calc_time, last_funding_rate)."""
    root = paths().parquet / DATASETS["um_funding_rate"].dir_slug
    wanted = set(symbols)
    frames = []
    for path in sorted(root.rglob("part-*.parquet")):
        sym = _symbol_of(path)
        if sym in wanted:
            frame = pl.read_parquet(path)
            frames.append(
                frame.select(
                    pl.col("calc_time"), pl.col("last_funding_rate"), pl.lit(sym)
                )
            )
    if not frames:
        raise SystemExit("no funding partitions — run quant convert um_funding_rate")
    return pl.concat(frames).rename({"literal": "symbol"}).sort(["symbol", "calc_time"])


def _closes_up_to(wide: pl.DataFrame, as_of: date, column: str) -> pl.Series:
    return wide.filter(pl.col(DATE_COL) <= as_of).sort(DATE_COL)[column].drop_nulls()


def _scoreable_columns(wide: pl.DataFrame) -> list[str]:
    return [c for c in wide.columns if c != DATE_COL]


def cross_sectional_momentum(
    wide: pl.DataFrame,
    as_of: date,
    lookback: int = 180,
    skip: int = 5,
    hold: int = 20,
    min_obs: int = MIN_OBS_DEFAULT,
) -> dict[str, float]:
    """Long the top `hold` by `lookback`-day return (ignoring the last
    `skip` days), equal-weighted; rest in cash. The "avoid losers" form:
    no short side, so the book carries only the durable long-winner leg."""
    scores: dict[str, float] = {}
    for column in _scoreable_columns(wide):
        closes = _closes_up_to(wide, as_of, column)
        if len(closes) >= max(min_obs, lookback + skip + 1):
            n = len(closes)
            past = closes[n - lookback - 1]
            recent = closes[n - skip - 2]
            if past and past > 0 and recent is not None:
                scores[column] = recent / past - 1.0
    winners = sorted(scores, key=lambda s: scores[s], reverse=True)[:hold]
    if not winners:
        return {}
    weight = 1.0 / len(winners)
    return {symbol: weight for symbol in winners}


def time_series_momentum(
    wide: pl.DataFrame,
    as_of: date,
    lookback: int = 90,
    vol_window: int = 30,
    target_vol: float = 0.20,
    min_obs: int = MIN_OBS_DEFAULT,
) -> dict[str, float]:
    """Sign of the `lookback`-day trend per symbol, each leg vol-scaled
    toward `target_vol` annualized, whole book capped at gross 1. The
    benchmark factor — everything else must beat this after stressed
    costs."""
    legs: dict[str, tuple[float, float]] = {}
    for column in _scoreable_columns(wide):
        closes = _closes_up_to(wide, as_of, column)
        if len(closes) < max(min_obs, lookback + vol_window + 1):
            continue
        n = len(closes)
        trend = closes[n - 1] / closes[n - lookback - 1] - 1.0
        recent = closes[n - vol_window :]
        returns = (recent / recent.shift(1) - 1.0).drop_nulls()
        daily_vol = returns.std()
        if returns.len() >= 2 and isinstance(daily_vol, float) and daily_vol > 0:
            legs[column] = (trend, float(daily_vol) * (252**0.5))
    if not legs:
        return {}
    raw = {
        symbol: (1.0 if trend > 0 else -1.0) * min(1.0, target_vol / vol)
        for symbol, (trend, vol) in legs.items()
    }
    gross = sum(abs(w) for w in raw.values())
    scale = min(1.0, 1.0 / gross) if gross > 0 else 0.0
    return {symbol: w * scale for symbol, w in raw.items()}


def funding_reversal(
    funding: pl.DataFrame,
    as_of: date,
    window_days: int = 30,
    extreme: float = 0.9,
    min_rows: int = 90,
    fresh_days: int = 3,
) -> dict[str, float]:
    """Fast band: fade the funding tails. A symbol whose latest settled
    rate sits in the top `extreme` percentile of its own trailing
    `window_days` distribution is crowded long — short it; bottom
    percentile — long. Equal-weighted per side, gross 1, flat when
    nothing is extreme."""
    start = as_of - timedelta(days=window_days)
    stale_before = as_of - timedelta(days=fresh_days)
    longs: list[str] = []
    shorts: list[str] = []
    histories = funding.partition_by("symbol", as_dict=True, maintain_order=True)
    for key, history in histories.items():
        symbol = key[0] if isinstance(key, tuple) else key
        rows = history.filter(pl.col("calc_time").dt.date() <= as_of)
        if len(rows) < min_rows:
            continue
        window = rows.filter(pl.col("calc_time").dt.date() >= start)[
            "last_funding_rate"
        ]
        recent = rows.filter(pl.col("calc_time").dt.date() <= stale_before)
        if len(window) < 10 or len(recent) == 0:
            continue
        current = recent["last_funding_rate"][len(recent) - 1]
        window_rates = window.to_list()
        below = sum(1 for r in window_rates if r < current)
        tied = sum(1 for r in window_rates if r == current)
        percentile = (below + 0.5 * tied) / len(window_rates)
        if percentile >= extreme:
            shorts.append(symbol)
        elif percentile <= 1.0 - extreme:
            longs.append(symbol)
    positions: dict[str, float] = {}
    if longs:
        positions.update({s: 1.0 / (2 * len(longs)) for s in longs})
    if shorts:
        positions.update({s: -1.0 / (2 * len(shorts)) for s in shorts})
    return positions


#: uniform screening signature: (as-of data..., as_of, **params) -> weights.
#: slow band takes the price frame; fast band takes the funding frame.
SLOW_FACTORS: dict[str, Callable] = {
    "csm": cross_sectional_momentum,
    "tsm": time_series_momentum,
}
FAST_FACTORS: dict[str, Callable] = {"funding": funding_reversal}
FACTORS: dict[str, Callable] = {**SLOW_FACTORS, **FAST_FACTORS}
