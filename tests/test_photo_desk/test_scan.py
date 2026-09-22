from __future__ import annotations

import os
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from photo_desk.config import PhotoRootError
from photo_desk.library import STATE_IN_PLACE, STATE_MISSING, Base, Photo
from photo_desk.scan import scan
from storage import SqliteClient

from .helpers import make_photo, make_settings


@pytest.fixture()
def env(tmp_path):
    settings = make_settings(tmp_path)
    client = SqliteClient(settings.db_path)
    client.init_schema(Base.metadata)
    yield settings, client
    client.dispose()


def photos_of(session: Session) -> dict[str, Photo]:
    return {p.rel_path: p for p in session.scalars(select(Photo))}


def test_scan_indexes_tree_and_skips_non_images(env) -> None:
    settings, client = env
    make_photo(
        settings.root / "2025" / "春游" / "IMG_0001.jpg",
        taken_at=datetime(2025, 4, 5, 10, 0, 0),
    )
    make_photo(settings.root / "2025" / "shot.png", taken_at=None)
    (settings.root / "notes.txt").write_text("not a photo")

    with client.session() as session:
        stats = scan(settings, session)
        assert (stats.new, stats.unchanged) == (2, 0)
        rows = photos_of(session)
        assert set(rows) == {"2025/春游/IMG_0001.jpg", "2025/shot.png"}
        jpg = rows["2025/春游/IMG_0001.jpg"]
        assert jpg.taken_at == datetime(2025, 4, 5, 10, 0)
        assert jpg.extension == "jpg"
        assert jpg.state == STATE_IN_PLACE


def test_rescan_is_idempotent_unchanged_only(env) -> None:
    settings, client = env
    make_photo(settings.root / "a.jpg", taken_at=datetime(2025, 1, 1))

    with client.session() as session:
        scan(settings, session)
    with client.session() as session:
        stats = scan(settings, session)
        assert (stats.new, stats.updated, stats.unchanged) == (0, 0, 1)


def test_touch_same_content_updates_fingerprint_but_keeps_exif(env) -> None:
    settings, client = env
    path = make_photo(settings.root / "a.jpg", taken_at=datetime(2025, 1, 1))
    with client.session() as session:
        scan(settings, session)
        before = photos_of(session)["a.jpg"].content_hash

    future = path.stat().st_mtime / 1.0 + 5.0
    os.utime(path, (future, future))
    with client.session() as session:
        stats = scan(settings, session)
        row = photos_of(session)["a.jpg"]
        assert stats.updated == 1 and stats.new == 0
        assert row.content_hash == before  # 内容未变
        assert row.taken_at == datetime(2025, 1, 1)  # EXIF 未被抹掉
        assert row.file_mtime_ns == os.stat(path).st_mtime_ns  # 指纹已刷新


def test_content_change_reindexes(env) -> None:
    settings, client = env
    path = make_photo(settings.root / "a.jpg", taken_at=datetime(2025, 1, 1))
    with client.session() as session:
        scan(settings, session)

    make_photo(
        path,
        taken_at=datetime(2026, 2, 2, 3, 4, 5),
        size=(300, 300),
        color="green",
    )
    with client.session() as session:
        stats = scan(settings, session)
        row = photos_of(session)["a.jpg"]
        assert stats.updated == 1
        assert row.taken_at == datetime(2026, 2, 2, 3, 4, 5)
        assert row.width == 300


def test_disappear_and_recover(env) -> None:
    settings, client = env
    path = make_photo(settings.root / "a.jpg", taken_at=datetime(2025, 1, 1))
    with client.session() as session:
        scan(settings, session)

    path.rename(settings.root / "a.jpg.bak")
    with client.session() as session:
        stats = scan(settings, session)
        assert stats.missing == 1
        assert photos_of(session)["a.jpg"].state == STATE_MISSING

    (settings.root / "a.jpg.bak").rename(path)
    with client.session() as session:
        stats = scan(settings, session)
        assert stats.recovered == 1
        assert photos_of(session)["a.jpg"].state == STATE_IN_PLACE


def test_blurred_dir_is_not_scanned(env) -> None:
    settings, client = env
    make_photo(settings.root / "blurred" / "junk.jpg")
    make_photo(settings.root / "keep.jpg")
    with client.session() as session:
        stats = scan(settings, session)
        assert stats.new == 1
        assert set(photos_of(session)) == {"keep.jpg"}


def test_scan_refuses_unmounted_root(env) -> None:
    settings, client = env
    missing_root = settings.root.parent / "unmounted"
    from photo_desk.config import load_settings

    broken = load_settings(missing_root, settings.data_dir)
    with client.session() as session:
        with pytest.raises(PhotoRootError):
            scan(broken, session)
