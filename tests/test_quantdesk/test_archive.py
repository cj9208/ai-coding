"""Download layer: URL shapes pinned by the live probe, diff-sync,
checksums, inventory."""

from __future__ import annotations

import io
import zipfile

import pytest

from quantdesk import archive
from quantdesk.archive import ChecksumMismatch, PlannedFile, RemoteMissing
from quantdesk.config import DATASETS


def _zip_bytes(name: str, body: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive_writer:
        archive_writer.writestr(name, body)
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, content: bytes) -> None:
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self) -> None:
        return None


@pytest.fixture
def served(monkeypatch: pytest.MonkeyPatch):
    """Fake ``http_get``: routes URL -> bytes, 404 via RemoteMissing."""
    routes: dict[str, bytes] = {}

    def fake_get(url: str, timeout: float = 120.0) -> FakeResponse:
        if url not in routes:
            raise RemoteMissing(url)
        return FakeResponse(routes[url])

    monkeypatch.setattr(archive, "http_get", fake_get)
    return routes


def test_klines_url_nests_interval_after_symbol() -> None:
    planned = PlannedFile(DATASETS["um_klines_1m"], "BTCUSDT", "monthly", "2024-06")
    assert (
        planned.url == "https://data.binance.vision/data/futures/um/monthly/klines"
        "/BTCUSDT/1m/BTCUSDT-1m-2024-06.zip"
    )


def test_funding_url_has_no_interval_and_monthly_only() -> None:
    planned = PlannedFile(DATASETS["um_funding_rate"], "BTCUSDT", "monthly", "2024-06")
    assert planned.filename == "BTCUSDT-fundingRate-2024-06.zip"
    assert not DATASETS["um_funding_rate"].daily


def test_months_between_inclusive() -> None:
    assert archive.months_between("2024-11", "2025-02") == [
        "2024-11",
        "2024-12",
        "2025-01",
        "2025-02",
    ]


def test_download_verifies_checksum_and_records_inventory(qdesk, served) -> None:
    body = "1717200000000,67540.01,1,1,1,1,1717200059999,1,1,1,1,0\n"
    data = _zip_bytes("BTCUSDT-1m-2024-06.csv", body)
    import hashlib

    digest = hashlib.sha256(data).hexdigest()
    url = PlannedFile(DATASETS["um_klines_1m"], "BTCUSDT", "monthly", "2024-06").url
    served[url] = data
    served[url + ".CHECKSUM"] = f"{digest}  BTCUSDT-1m-2024-06.zip\n".encode()

    actions = archive.download(
        DATASETS["um_klines_1m"],
        ["BTCUSDT"],
        "2024-06",
        "2024-06",
        include_daily=False,
    )
    assert [a.outcome for a in actions] == ["fetched"]
    inventory = archive.load_inventory()
    (record,) = inventory.values()
    assert record.sha256 == digest and record.remote_sha256 == digest

    # second pass is a pure skip — no HTTP for the zip itself
    served.clear()
    actions = archive.download(
        DATASETS["um_klines_1m"],
        ["BTCUSDT"],
        "2024-06",
        "2024-06",
        include_daily=False,
    )
    assert [a.outcome for a in actions] == ["skipped"]


def test_download_rejects_bad_bytes(qdesk, served) -> None:
    data = _zip_bytes("x.csv", "junk\n")
    url = PlannedFile(DATASETS["um_klines_1m"], "BTCUSDT", "monthly", "2024-06").url
    served[url] = data
    served[url + ".CHECKSUM"] = b"0" * 64 + b"  x.zip\n"
    with pytest.raises(ChecksumMismatch):
        archive.download(
            DATASETS["um_klines_1m"],
            ["BTCUSDT"],
            "2024-06",
            "2024-06",
            include_daily=False,
        )


def test_absent_archive_is_a_gap_not_an_error(qdesk, served) -> None:
    actions = archive.download(
        DATASETS["um_klines_1m"],
        ["NOSUCHCOIN"],
        "2024-06",
        "2024-06",
        include_daily=False,
    )
    assert [a.outcome for a in actions] == ["missing"]
    assert archive.load_inventory() == {}
