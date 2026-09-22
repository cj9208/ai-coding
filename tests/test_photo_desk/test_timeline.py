from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from photo_desk.app import create_app
from photo_desk.library import Base
from photo_desk.scan import scan
from storage import SqliteClient

from .helpers import make_photo, make_settings


@pytest.fixture()
def client(tmp_path):
    settings = make_settings(tmp_path)
    make_photo(
        settings.root / "2025" / "春游.jpg",
        taken_at=datetime(2025, 4, 5, 10, 0),
    )
    make_photo(
        settings.root / "2025" / "生日.jpg",
        taken_at=datetime(2025, 4, 20, 18, 30),
        color="purple",
    )
    make_photo(settings.root / "无exif.png", taken_at=None)
    (settings.root / "blurred").mkdir()
    make_photo(settings.root / "blurred" / "废片.jpg", color="gray")

    storage = SqliteClient(settings.db_path)
    storage.init_schema(Base.metadata)
    with storage.session() as session:
        scan(settings, session)
    storage.dispose()
    return TestClient(create_app(settings))


def test_timeline_groups_by_exif_day(client) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "2025-04-20" in html and "2025-04-05" in html
    assert html.index("2025-04-20") < html.index("2025-04-05")  # 新→旧
    # 无 EXIF 的 png 也要出现（回退文件时间），blurred/ 里的照片不在时间线
    assert html.count("cell") >= 3


def test_photo_detail_and_derived_images(client) -> None:
    html = client.get("/").text
    # 取第一张照片的详情链接
    import re

    ids = sorted({int(m) for m in re.findall(r"/photo/(\d+)", html)})
    assert ids, "时间线里应有照片链接"
    pid = ids[0]

    detail = client.get(f"/photo/{pid}")
    assert detail.status_code == 200
    assert "SHA-256" in detail.text

    thumb = client.get(f"/photo/{pid}/thumb.webp")
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert len(thumb.content) > 100

    # 二次请求走缓存，内容一致
    assert client.get(f"/photo/{pid}/thumb.webp").content == thumb.content


def test_missing_photo_404(client) -> None:
    assert client.get("/photo/9999").status_code == 404
    assert client.get("/photo/9999/thumb.webp").status_code == 404


def test_quarantined_cells_are_grayed_out(tmp_path) -> None:
    from photo_desk.triage import apply_plan, build_plan

    from .helpers import make_burst_photo

    settings = make_settings(tmp_path)
    base = datetime(2025, 6, 1, 12, 0)
    make_burst_photo(
        settings.root / "b" / "IMG_0001.jpg", taken_at=base, subsec="000", seed=1
    )
    make_burst_photo(
        settings.root / "b" / "IMG_0002.jpg",
        taken_at=base,
        subsec="300",
        seed=2,
        blur=6,
    )
    storage = SqliteClient(settings.db_path)
    storage.init_schema(Base.metadata)
    with storage.session() as session:
        scan(settings, session)
        report = build_plan(settings, session)
        assert apply_plan(settings, session, report.moves) == 1
    storage.dispose()

    c = TestClient(create_app(settings))
    html = c.get("/").text
    assert "cell quarantined" in html and "已隔离" in html
    assert "photos restore --photo" in html  # 悬停提示给出放回命令


def test_unmounted_root_shows_error_not_empty(tmp_path) -> None:
    from photo_desk.config import load_settings

    settings = load_settings(tmp_path / "unmounted-drive", tmp_path / "out")
    c = TestClient(create_app(settings))
    resp = c.get("/")
    assert resp.status_code == 200
    assert "不可访问" in resp.text  # 不是静默空页
