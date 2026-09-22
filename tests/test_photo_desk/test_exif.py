from __future__ import annotations

from datetime import datetime

import pytest

from photo_desk.exif import dms_to_degrees, parse_datetime, read_exif

from .helpers import make_photo


def test_parse_datetime_with_subsec() -> None:
    assert parse_datetime("2025:08:31 14:02:03", "45") == datetime(
        2025, 8, 31, 14, 2, 3, 450000
    )


def test_parse_datetime_bad_values_degrade_to_none() -> None:
    assert parse_datetime("") is None
    assert parse_datetime(None) is None
    assert parse_datetime("not a date") is None
    # 日期合法但秒非法 → 整体 None，不猜
    assert parse_datetime("2025:13:45 99:99:99") is None


def test_dms_to_degrees() -> None:
    assert dms_to_degrees("N", 30, 15, 0) == pytest.approx(30.25)
    assert dms_to_degrees("S", 30, 15, 0) == pytest.approx(-30.25)
    assert dms_to_degrees("W", 120, 5, 30) == pytest.approx(-120.0916666, abs=1e-5)
    assert dms_to_degrees("E", "x", None, None) is None


def test_read_exif_roundtrip(tmp_path) -> None:
    jpg = make_photo(
        tmp_path / "a.jpg",
        taken_at=datetime(2024, 3, 1, 8, 5, 6),
        subsec="120",
        make="Canon",
        model="EOS R6",
        gps=(22.5, -120.125),
        size=(640, 480),
    )
    data = read_exif(jpg)
    assert data.taken_at == datetime(2024, 3, 1, 8, 5, 6, 120000)
    assert (data.make, data.model) == ("Canon", "EOS R6")
    assert data.width == 640 and data.height == 480
    assert data.gps_lat == pytest.approx(22.5, abs=1e-4)
    assert data.gps_lng == pytest.approx(-120.125, abs=1e-4)


def test_read_exif_png_without_exif(tmp_path) -> None:
    png = make_photo(tmp_path / "shot.png", taken_at=None)
    data = read_exif(png)
    assert data.taken_at is None
    assert data.width == 200  # 尺寸仍可得


def test_read_exif_corrupt_file_degrades(tmp_path) -> None:
    bad = tmp_path / "broken.jpg"
    bad.write_bytes(b"\xff\xd8\xff not really a jpeg")
    data = read_exif(bad)
    assert data.taken_at is None
