"""Coverage and integrity: what ``quant list`` and ``quant verify`` read.

Three checks, in the order their failure modes bite:

1. **local** — every archived zip still hashes to what the inventory
   recorded when it was fetched (disk rot / manual tampering);
2. **remote** (``--remote``) — the archive's current upstream checksum
   vs the recorded one: a difference is not an error to fix silently,
   it is the *replacement event* the plan cares about, so it is
   reported as ``REPLACED`` and left for ``download --refresh`` to act
   on;
3. **grid** — for time-indexed datasets, a month whose rows don't fill
   ``[min..max]`` at the dataset's step is missing bars; the check is
   (``span // step + 1 == rows``), which is exact for klines and
   funding.

Partitions are read from the hive layout directly (``symbol=X/year=Y/
month=Z`` directories), so coverage needs no catalog — the filesystem
*is* the index, matching the "Parquet is the source of truth" rule.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import polars as pl

from .archive import fetch_checksum, load_inventory, sha256_file
from .config import DATASETS, Dataset, paths


@dataclass
class SymbolCoverage:
    dataset: str
    symbol: str
    months: list[str] = field(default_factory=list)
    rows: int = 0

    @property
    def span(self) -> str:
        if not self.months:
            return "-"
        return f"{self.months[0]}..{self.months[-1]}"


def partition_root(dataset: Dataset, symbol: str) -> Path:
    return paths().parquet / dataset.dir_slug / f"symbol={symbol}"


def scan_coverage() -> list[SymbolCoverage]:
    """Every (dataset, symbol) that has at least one built month."""
    out: list[SymbolCoverage] = []
    for dataset in DATASETS.values():
        ds_root = paths().parquet / dataset.dir_slug
        if not ds_root.is_dir():
            continue
        for symbol_dir in sorted(ds_root.glob("symbol=*")):
            symbol = symbol_dir.name.removeprefix("symbol=")
            months: list[str] = []
            rows = 0
            for part in sorted(symbol_dir.glob("*/*/*.parquet")):
                year = part.parent.parent.name.removeprefix("year=")
                month = part.parent.name.removeprefix("month=")
                months.append(f"{year}-{month}")
                rows += pl.read_parquet(part).height
            out.append(SymbolCoverage(dataset.name, symbol, months, rows))
    return out


@dataclass
class Issue:
    kind: str  # "local-hash" | "remote" | "grid" | "inventory"
    subject: str
    detail: str


def check_local_hashes() -> list[Issue]:
    issues: list[Issue] = []
    for key, record in load_inventory().items():
        local = paths().raw / record.path
        if not local.is_file():
            issues.append(Issue("inventory", key, "recorded but file absent"))
        elif sha256_file(local) != record.sha256:
            issues.append(Issue("local-hash", key, "bytes no longer match inventory"))
    return issues


def check_replacements() -> list[Issue]:
    """Upstream checksum vs recorded — the archive-drift detector."""
    issues: list[Issue] = []
    for key, record in load_inventory().items():
        url = f"https://data.binance.vision/{record.path}"
        remote = fetch_checksum(url)
        if remote is None:
            issues.append(Issue("remote", key, "archive no longer served upstream"))
        elif record.remote_sha256 is not None and remote != record.remote_sha256:
            issues.append(
                Issue(
                    "remote",
                    key,
                    f"REPLACED upstream: {remote[:12]} != "
                    f"{record.remote_sha256[:12]} — re-download with --refresh",
                )
            )
    return issues


def check_grid(dataset: Dataset, symbol: str, months: list[str]) -> list[Issue]:
    """Row-vs-span continuity per built month (time-indexed datasets)."""
    issues: list[Issue] = []
    if dataset.step_us == 0:
        return issues
    for month in months:
        year, mm = month.split("-")
        part = (
            partition_root(dataset, symbol)
            / f"year={year}"
            / f"month={mm}"
            / (f"part-{month}.parquet")
        )
        if not part.is_file():
            continue
        frame = pl.read_parquet(part, columns=[dataset.time_col])
        if frame.height == 0:
            issues.append(Issue("grid", f"{dataset.name}/{symbol}/{month}", "empty"))
            continue
        first, last = frame[dataset.time_col][0], frame[dataset.time_col][-1]
        expected = (last - first) // timedelta(microseconds=dataset.step_us) + 1
        if expected != frame.height:
            issues.append(
                Issue(
                    "grid",
                    f"{dataset.name}/{symbol}/{month}",
                    f"{frame.height} rows, {expected} expected for span "
                    "(missing bars)",
                )
            )
    return issues


def verify(remote: bool = False) -> list[Issue]:
    issues = check_local_hashes()
    for coverage in scan_coverage():
        dataset = DATASETS[coverage.dataset]
        issues.extend(check_grid(dataset, coverage.symbol, coverage.months))
    if remote:
        issues.extend(check_replacements())
    return issues
