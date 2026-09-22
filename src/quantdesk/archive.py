"""The download layer over ``data.binance.vision``.

Everything here is derived from two verified facts. The archive layout
(live-probed 2026-09-22):

    data/<market>/<granularity>/<kind>[/<interval>]/<symbol>/<name>.zip
    e.g. data/spot/monthly/klines/BTCUSDT/1m/BTCUSDT-1m-2024-06.zip
         data/futures/um/monthly/fundingRate/BTCUSDT/BTCUSDT-fundingRate-2024-06.zip

and every ``.zip`` has a ``.zip.CHECKSUM`` holding its sha256 — which
matters because **upstream replaces archives after the fact** (the
``updates/`` CHANGELOG is proof), so "downloaded once" is never
"durable": each file is stored with the checksum *as observed at fetch
time* in ``inventory.jsonl``, and a later refresh that sees a
different remote checksum records the replacement instead of silently
swapping bytes (freqtrade-style "same query, two results" bugs die
here).

Download is a diff-sync: plan the file set, skip what is local and
still matches the inventory, fetch what doesn't. 404s are *gaps*, not
errors — they are reported, because a missing delisted-pair month is
exactly the information the survivorship discussion needs.
"""

from __future__ import annotations

import hashlib
import json
import sys
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, NamedTuple

import httpx
from tenacity import (
    retry,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .config import Dataset, paths

BASE_URL = "https://data.binance.vision"


class RemoteMissing(Exception):
    """404 — the archive simply does not contain this file."""


class ChecksumMismatch(Exception):
    """Downloaded bytes are not what upstream's .CHECKSUM says."""


@dataclass(frozen=True)
class PlannedFile:
    dataset: Dataset
    symbol: str
    granularity: str  # "monthly" | "daily"
    stamp: str  # "2024-06" or "2024-06-03"

    @property
    def key(self) -> str:
        return f"{self.dataset.name}/{self.symbol}/{self.granularity}/{self.stamp}"

    @property
    def filename(self) -> str:
        parts = [self.symbol]
        if self.dataset.interval:
            parts.append(self.dataset.interval)
        else:
            parts.append(self.dataset.kind)
        parts.append(self.stamp)
        return "-".join(parts) + ".zip"

    @property
    def relative_path(self) -> str:
        """Path inside the archive — also our ``raw/`` layout.
        (klines nest one deeper *after* the symbol: kind/symbol/interval)"""
        parts = ["data", self.dataset.market, self.granularity, self.dataset.kind]
        parts.append(self.symbol)
        if self.dataset.interval:
            parts.append(self.dataset.interval)
        parts.append(self.filename)
        return "/".join(parts)

    @property
    def url(self) -> str:
        return f"{BASE_URL}/{self.relative_path}"

    @property
    def local_path(self) -> Path:
        return paths().raw / self.relative_path


@dataclass
class InventoryRecord:
    key: str
    path: str
    sha256: str
    bytes: int
    fetched_at: str
    remote_sha256: str | None = None  # checksum observed at fetch time
    replaced_at: str | None = None  # set when a later refresh saw a new hash


class PlannedFileKey(NamedTuple):
    dataset: str
    symbol: str
    granularity: str
    stamp: str


def load_inventory() -> dict[str, InventoryRecord]:
    """Last-write-wins view of ``inventory.jsonl``."""
    file = paths().inventory
    records: dict[str, InventoryRecord] = {}
    if not file.is_file():
        return records
    for line in file.read_text(encoding="utf-8").splitlines():
        if line.strip():
            record = InventoryRecord(**json.loads(line))
            records[record.key] = record
    return records


def append_inventory(records: list[InventoryRecord]) -> None:
    file = paths().inventory
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record.__dict__) + "\n")


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


@retry(
    reraise=True,
    stop=stop_after_attempt(3),
    # a 404 is a durable answer, not a transient failure — retrying it
    # tripled the cost of every missing month during the backfill
    retry=retry_if_not_exception_type(RemoteMissing),
    wait=wait_exponential(multiplier=1, min=1, max=10),
)
def http_get(url: str, timeout: float = 120.0) -> httpx.Response:
    """One seam for the whole network surface — tests fake this."""
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    if response.status_code == 404:
        raise RemoteMissing(url)
    response.raise_for_status()
    return response


def fetch_checksum(url: str) -> str | None:
    """The sha256 upstream currently claims for ``url``; None if absent."""
    try:
        body = http_get(url + ".CHECKSUM", timeout=30.0).text
    except RemoteMissing:
        return None
    return body.split()[0]


def months_between(since: str, until: str) -> list[str]:
    """Inclusive "YYYY-MM" range, e.g. ("2024-01", "2024-03")."""
    start = datetime.strptime(since, "%Y-%m")
    end = datetime.strptime(until, "%Y-%m")
    months: list[str] = []
    cursor = start
    while cursor <= end:
        months.append(cursor.strftime("%Y-%m"))
        cursor = datetime(
            cursor.year + (cursor.month == 12), (cursor.month % 12) + 1, 1
        )
    return months


def days_of(month: str) -> list[str]:
    """Every day of "YYYY-MM" up to yesterday — daily archives land T+1."""
    first = datetime.strptime(month, "%Y-%m")
    last_day = monthrange(first.year, first.month)[1]
    today = date.today()
    days = []
    for day in range(1, last_day + 1):
        stamp = date(first.year, first.month, day)
        if (today - stamp).days >= 1:
            days.append(stamp.strftime("%Y-%m-%d"))
    return days


def plan(
    dataset: Dataset,
    symbols: list[str],
    since: str,
    until: str,
    include_daily: bool = True,
) -> list[PlannedFile]:
    """Monthly files for [since..until]; daily files cover what monthly
    cannot have yet (the current month) when asked."""
    files: list[PlannedFile] = []
    for symbol in symbols:
        for month in months_between(since, until):
            if dataset.monthly:
                files.append(PlannedFile(dataset, symbol, "monthly", month))
            if include_daily and dataset.daily and not _month_complete(month):
                files.extend(
                    PlannedFile(dataset, symbol, "daily", day) for day in days_of(month)
                )
    return files


def _month_complete(month: str) -> bool:
    """Monthly archives are published the first Monday after month end;
    until then the month can only be assembled from daily files."""
    first = datetime.strptime(month, "%Y-%m")
    next_month = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    first_monday = next_month + timedelta(days=(0 - next_month.weekday()) % 7)
    return date.today() >= first_monday.date()


@dataclass
class DownloadAction:
    planned: PlannedFile
    outcome: str  # "fetched" | "skipped" | "replaced" | "missing" | "mismatch"
    sha256: str = ""


def fetch(planned: PlannedFile, refresh_remote: bool) -> DownloadAction:
    """Bring one planned file local, honoring the recorded-checksum rules."""
    inventory = load_inventory()
    record = inventory.get(planned.key)
    local = planned.local_path
    if refresh_remote:
        remote = fetch_checksum(planned.url)
        if remote is None:
            return DownloadAction(planned, "missing")
        if record and local.is_file() and record.sha256 == sha256_file(local):
            if record.remote_sha256 == remote:
                return DownloadAction(planned, "skipped", record.sha256)
            # upstream replaced the archive: refetch, then record the fact
            data = http_get(planned.url).content
            digest = sha256_of(data)
            if digest != remote:
                raise ChecksumMismatch(planned.url)
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_bytes(data)
            _record(planned, record, digest, len(data), replaced=True)
            return DownloadAction(planned, "replaced", digest)
    if record and local.is_file() and sha256_file(local) == record.sha256:
        return DownloadAction(planned, "skipped", record.sha256)
    data = http_get(planned.url).content
    digest = sha256_of(data)
    remote = fetch_checksum(planned.url)
    if remote is not None and digest != remote:
        raise ChecksumMismatch(planned.url)
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_bytes(data)
    _record(planned, record, digest, len(data), remote)
    return DownloadAction(planned, "fetched", digest)


def _record(
    planned: PlannedFile,
    previous: InventoryRecord | None,
    digest: str,
    size: int,
    remote_sha256: str | None = None,
    replaced: bool = False,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    append_inventory(
        [
            InventoryRecord(
                key=planned.key,
                path=planned.relative_path,
                sha256=digest,
                bytes=size,
                fetched_at=now,
                remote_sha256=(
                    remote_sha256
                    if not replaced
                    else (previous.remote_sha256 if previous else None)
                ),
                replaced_at=(
                    now if replaced else (previous.replaced_at if previous else None)
                ),
            )
        ]
    )


def download(
    dataset: Dataset,
    symbols: list[str],
    since: str,
    until: str,
    *,
    include_daily: bool = True,
    refresh_remote: bool = False,
    on_action: Callable[[DownloadAction], None] | None = None,
) -> list[DownloadAction]:
    """Diff-sync one dataset; the caller owns the progress rendering."""
    actions: list[DownloadAction] = []
    for planned in plan(dataset, symbols, since, until, include_daily):
        try:
            action = fetch(planned, refresh_remote)
        except RemoteMissing:
            action = DownloadAction(planned, "missing")
        actions.append(action)
        if on_action:
            on_action(action)
        elif action.outcome in ("fetched", "replaced", "missing", "mismatch"):
            print(
                f"{action.outcome:<9} {planned.key}",
                file=sys.stderr,
            )
    return actions
