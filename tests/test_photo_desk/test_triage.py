"""M1 写通道：组内排名 → dry-run 计划 → --apply 隔离 → 验身放回。

样张必须用 make_burst_photo（噪声底纹）：纯色图全 0 分，相对排名不成立。
"""

from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import select

from photo_desk.library import (
    STATE_IN_PLACE,
    STATE_QUARANTINED,
    MoveLedger,
    Photo,
    ensure_schema,
)
from photo_desk.scan import scan
from photo_desk.triage import apply_plan, build_plan, restore
from storage import SqliteClient

from .helpers import make_burst_photo, make_photo, make_settings

BASE = datetime(2025, 6, 1, 12, 0, 0)


@pytest.fixture()
def env(tmp_path):
    settings = make_settings(tmp_path)
    client = SqliteClient(settings.db_path)
    ensure_schema(client)
    yield settings, client
    client.dispose()


def build_burst(settings, client) -> dict[str, Photo]:
    """3 张连拍（两张清晰 + 一张高斯模糊）+ 1 张独张模糊 + 1 张无 EXIF。"""
    burst = settings.root / "burst"
    make_burst_photo(burst / "IMG_0001.jpg", taken_at=BASE, subsec="000", seed=1)
    make_burst_photo(burst / "IMG_0002.jpg", taken_at=BASE, subsec="200", seed=2)
    make_burst_photo(
        burst / "IMG_0003.jpg", taken_at=BASE, subsec="400", seed=3, blur=5
    )
    make_photo(
        settings.root / "solo.jpg",
        color="black",
        taken_at=datetime(2025, 6, 2, 9, 0),
    )
    with client.session() as session:
        scan(settings, session)
        return {p.rel_path: p for p in session.scalars(select(Photo))}


def test_apply_moves_files_and_writes_ledger(env) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    original_bytes = (settings.root / "burst" / "IMG_0003.jpg").read_bytes()

    with client.session() as session:
        report = build_plan(settings, session)
        assert apply_plan(settings, session, report.moves) == 1
        row = session.get(Photo, pid)
        assert row.state == STATE_QUARANTINED
        assert row.rel_path.startswith("blurred/")
        assert row.rel_path.endswith("_IMG_0003.jpg")
        entry = session.scalar(select(MoveLedger))
        assert entry.rel_from == "burst/IMG_0003.jpg"
        assert entry.rel_to == row.rel_path
        assert entry.sha256 == row.content_hash
        assert entry.reason == "blur_rank" and entry.group_id == row.group_id
        # 原件字节不变（D-5）：移动前后同哈希
        moved = settings.photo_path(row.rel_path)
        assert moved.read_bytes() == original_bytes
    assert not (settings.root / "burst" / "IMG_0003.jpg").exists()


def test_plan_quarantines_only_blurred_burst_member(env) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    with client.session() as session:
        report = build_plan(settings, session)
        assert [m.rel_from for m in report.moves] == ["burst/IMG_0003.jpg"]
        assert report.groups == 1
        # 评分与组 id 已惰性写回
        blurred = session.get(Photo, rows["burst/IMG_0003.jpg"].id)
        assert blurred.sharpness is not None and blurred.group_id
        sharp = session.get(Photo, rows["burst/IMG_0001.jpg"].id)
        assert sharp.sharpness > 2 * blurred.sharpness
        assert sharp.group_id == blurred.group_id
        # 独张不参与自动判定（§4.4）
        assert session.get(Photo, rows["solo.jpg"].id).group_id is None
    # dry-run 不动文件、不写账本
    assert (settings.root / "burst" / "IMG_0003.jpg").is_file()
    with client.session() as session:
        assert session.scalar(select(MoveLedger).limit(1)) is None


def test_restore_single_photo(env) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    with client.session() as session:
        report = build_plan(settings, session)
        apply_plan(settings, session, report.moves)
        outcomes = restore(settings, session, photo_id=pid)
    assert len(outcomes) == 1 and outcomes[0].restored
    assert (settings.root / "burst" / "IMG_0003.jpg").is_file()
    with client.session() as session:
        row = session.get(Photo, pid)
        assert row.state == STATE_IN_PLACE
        assert row.rel_path == "burst/IMG_0003.jpg"
        entry = session.scalar(select(MoveLedger))
        assert entry.restored_at is not None and entry.restore_error is None
    # 放回后无待放回记录
    with client.session() as session:
        assert restore(settings, session, photo_id=pid) == []


def test_restore_group_and_recreate_parent(env) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    with client.session() as session:
        report = build_plan(settings, session)
        apply_plan(settings, session, report.moves)
        gid = session.get(Photo, rows["burst/IMG_0003.jpg"].id).group_id
    # 模拟"整棵相册目录被人手删了"：先清掉剩下的好片，再删空目录
    (settings.root / "burst" / "IMG_0001.jpg").unlink()
    (settings.root / "burst" / "IMG_0002.jpg").unlink()
    (settings.root / "burst").rmdir()
    with client.session() as session:
        outcomes = restore(settings, session, group_id=gid)
    assert len(outcomes) == 1
    assert outcomes[0].restored and "recreated_parent" in outcomes[0].detail
    assert (settings.root / "burst" / "IMG_0003.jpg").is_file()


def test_restore_rejects_tampered_bytes(env) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    with client.session() as session:
        report = build_plan(settings, session)
        apply_plan(settings, session, report.moves)
        blurred_rel = session.get(Photo, pid).rel_path
    settings.photo_path(blurred_rel).write_bytes(b"someone re-saved this")
    with client.session() as session:
        outcomes = restore(settings, session, photo_id=pid)
    assert not outcomes[0].restored
    assert "sha256" in outcomes[0].detail
    assert settings.photo_path(blurred_rel).is_file()  # 留在原地不放回
    with client.session() as session:
        entry = session.scalar(select(MoveLedger))
        assert entry.restored_at is None and "sha256" in entry.restore_error
        assert session.get(Photo, pid).state == STATE_QUARANTINED


def test_restore_refuses_to_overwrite_occupied_slot(env) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    with client.session() as session:
        report = build_plan(settings, session)
        apply_plan(settings, session, report.moves)
    (settings.root / "burst" / "IMG_0003.jpg").write_bytes(b"occupant")
    with client.session() as session:
        outcomes = restore(settings, session, photo_id=pid)
    assert not outcomes[0].restored and "占用" in outcomes[0].detail
    assert (settings.root / "burst" / "IMG_0003.jpg").read_bytes() == b"occupant"


def test_member_that_cannot_decode_has_no_verdict(env) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0002.jpg"].id
    # scan 之后文件从盘上消失（SMB 抖动/人手挪走）：无分数、不错杀
    (settings.root / "burst" / "IMG_0002.jpg").unlink()
    with client.session() as session:
        report = build_plan(settings, session)
        assert report.unscored == 1
        assert [m.rel_from for m in report.moves] == ["burst/IMG_0003.jpg"]
        assert session.get(Photo, pid).sharpness is None


def test_second_triage_after_apply_is_empty(env) -> None:
    settings, client = env
    build_burst(settings, client)
    with client.session() as session:
        report = build_plan(settings, session)
        assert report.scored == 3  # 三张连拍全部评分（独张不入组）
        apply_plan(settings, session, report.moves)
        again = build_plan(settings, session)
        assert again.moves == []
        # 隔离那张退出 in_place；剩下的两张仍成组、无垫底
        assert again.scored == 2
