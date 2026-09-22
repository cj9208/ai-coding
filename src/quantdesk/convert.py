"""Archive CSV -> normalized Parquet: where "source of truth" is earned.

One output file per (dataset, symbol, month) under the hive layout
``parquet/<dataset>/symbol=SYM/year=YYYY/month=MM/part-<month>.parquet``.
Monthly sources win over dailies for the same month (a month is
re-written whole when its monthly archive arrives, so daily parts can
never coexist with monthly ones), rows are sorted and de-duplicated —
and because the write is a pure function of the verified raw bytes,
"same month, two different answers" can only mean upstream replaced
the archive, which the inventory already flags.

Two live-probed format facts are absorbed here so nothing downstream
ever sees them: timestamps are epoch **milliseconds before 2025 and
microseconds from 2025-01** (unit sniffed per file by magnitude), and
klines/aggTrades files carry no header row while fundingRate files do
(sniffed by whether the first field parses as a number). Everything is
stored as tz-aware UTC datetimes.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from .archive import InventoryRecord, load_inventory
from .config import Dataset, paths

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "ignore",
]
AGG_TRADE_COLUMNS = [
    "agg_trade_id",
    "price",
    "quantity",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "is_buyer_maker",
    "is_best_match",
]
FUNDING_COLUMNS = ["calc_time", "funding_interval_hours", "last_funding_rate"]

_FLOATS = {
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "price",
    "quantity",
    "last_funding_rate",
    "funding_interval_hours",
}
_INTS = {
    "open_time",
    "close_time",
    "trade_count",
    "agg_trade_id",
    "first_trade_id",
    "last_trade_id",
    "transact_time",
    "calc_time",
}

MICROSECONDS_CUTOFF = 1_000_000_000_000_000  # 1e15 separates ms from us


def _columns(dataset: Dataset) -> list[str]:
    if dataset.kind == "klines":
        return KLINE_COLUMNS
    if dataset.kind == "aggTrades":
        return AGG_TRADE_COLUMNS
    if dataset.kind == "fundingRate":
        return FUNDING_COLUMNS
    raise ValueError(f"no CSV schema for kind {dataset.kind!r}")


def _sniff_header(text: str) -> bool:
    return not text.split("\n", 1)[0].split(",")[0].isdigit()


def read_csv(csv_bytes: bytes, dataset: Dataset) -> pl.DataFrame:
    """Typed, UTC-normalized frame for one archive's CSV member."""
    has_header = _sniff_header(csv_bytes.decode("ascii", "ignore"))
    schema = _columns(dataset)
    frame = pl.read_csv(
        io.BytesIO(csv_bytes),
        has_header=has_header,
        infer_schema_length=0,  # everything Utf8; we cast deliberately
    )
    if not has_header:
        if len(frame.columns) < len(schema):
            raise ValueError(
                f"{dataset.name}: {len(frame.columns)} fields, schema wants {len(schema)}"
            )
        frame = frame.rename(dict(zip(frame.columns, schema)))
    if "ignore" in frame.columns:
        frame = frame.drop("ignore")
    overrides: dict[str, Any] = {c: pl.Int64 for c in schema if c in _INTS}
    overrides.update({c: pl.Float64 for c in schema if c in _FLOATS})
    frame = frame.with_columns(
        *(pl.col(c).cast(overrides[c]) for c in overrides if c in frame.columns)
    )
    time_col = dataset.time_col
    max_raw = frame[time_col].max()
    unit_us = isinstance(max_raw, int) and max_raw >= MICROSECONDS_CUTOFF
    factor = 1 if unit_us else 1000
    return frame.with_columns(
        pl.from_epoch(pl.col(time_col) * factor, time_unit="us")
        .dt.replace_time_zone("UTC")
        .alias(time_col)
    ).sort(time_col)


def raw_csv(planned_path: str) -> bytes:
    """The single CSV member of a stored archive."""
    with zipfile.ZipFile(paths().raw / planned_path) as archive:
        member = next(n for n in archive.namelist() if n.endswith(".csv"))
        with archive.open(member) as handle:
            return handle.read()


def month_sources(
    dataset: Dataset, symbol: str, month: str, inventory: dict[str, InventoryRecord]
) -> list[InventoryRecord]:
    """Monthly record if fetched, else the daily records for that month."""
    prefix = f"{dataset.name}/{symbol}/"
    monthly = inventory.get(f"{prefix}monthly/{month}")
    if monthly is not None:
        return [monthly]
    return sorted(
        (
            r
            for r in inventory.values()
            if r.key.startswith(prefix + "daily/")
            and r.key.rsplit("/", 1)[-1].startswith(month)
        ),
        key=lambda r: r.key,
    )


@dataclass
class ConvertResult:
    dataset: str
    symbol: str
    month: str
    rows: int
    path: Path
    source: str  # "monthly" | "daily:<n> files"


def convert_month(
    dataset: Dataset,
    symbol: str,
    month: str,
    inventory: dict[str, InventoryRecord] | None = None,
) -> ConvertResult | None:
    """(Re)build one hive partition from verified raw bytes."""
    inventory = inventory if inventory is not None else load_inventory()
    sources = month_sources(dataset, symbol, month, inventory)
    if not sources:
        return None
    frames = [read_csv(raw_csv(r.path), dataset) for r in sources]
    frame = (
        frames[0]
        if len(frames) == 1
        else pl.concat(frames)
        .sort(dataset.time_col)
        .unique(subset=[dataset.time_col], keep="first")
    )
    year, mm = month.split("-")
    partition = (
        paths().parquet
        / dataset.dir_slug
        / f"symbol={symbol}"
        / f"year={year}"
        / f"month={mm}"
    )
    partition.mkdir(parents=True, exist_ok=True)
    for stale in partition.glob("part-*.parquet"):
        stale.unlink()  # daily parts make way when the monthly arrives
    out = partition / f"part-{month}.parquet"
    frame.write_parquet(out)
    return ConvertResult(
        dataset=dataset.name,
        symbol=symbol,
        month=month,
        rows=frame.height,
        path=out,
        source=(
            "monthly"
            if len(sources) == 1 and "monthly" in sources[0].key
            else f"daily:{len(sources)}"
        ),
    )
