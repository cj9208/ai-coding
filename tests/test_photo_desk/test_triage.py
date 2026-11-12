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
from photo_desk.triage import Move, apply_plan, build_plan, restore
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


@pytest.mark.parametrize("tamper", ["rel_path", "content_hash", "state", "deleted"])
def test_restore_rejects_photo_identity_tampering(env, tamper) -> None:
    """正常隔离后 Photo 行被人改过/删掉：业务拒绝返回 outcome，不抛异常，
    blurred 字节不动、账本保持 quarantined 并记 restore_error。"""
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    with client.session() as session:
        report = build_plan(settings, session)
        apply_plan(settings, session, report.moves)
        blurred_rel = session.get(Photo, pid).rel_path
    blurred_bytes = settings.photo_path(blurred_rel).read_bytes()
    with client.session() as session:
        row = session.get(Photo, pid)
        if tamper == "rel_path":
            row.rel_path = "elsewhere/IMG_0003.jpg"
        elif tamper == "content_hash":
            row.content_hash = "0" * 64
        elif tamper == "state":
            row.state = STATE_IN_PLACE
        else:
            session.delete(row)
        session.commit()
    with client.session() as session:
        outcomes = restore(settings, session, photo_id=pid)
    assert len(outcomes) == 1
    assert not outcomes[0].restored and outcomes[0].detail
    assert settings.photo_path(blurred_rel).read_bytes() == blurred_bytes
    assert not (settings.root / "burst" / "IMG_0003.jpg").exists()
    with client.session() as session:
        entry = session.scalar(select(MoveLedger))
        assert entry.phase == "quarantined"
        assert entry.restored_at is None
        assert entry.restore_error


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


def test_apply_commit_failure_keeps_recoverable_intent(env, monkeypatch):
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    with client.session() as session:
        report = build_plan(settings, session)
    mv = report.moves[0]
    original = settings.photo_path(mv.rel_from).read_bytes()
    with client.session() as session:
        commit = session.commit

        def fail_after_move():
            if settings.photo_path(mv.rel_to).exists():
                raise RuntimeError("injected commit failure")
            return commit()

        monkeypatch.setattr(session, "commit", fail_after_move)
        with pytest.raises(RuntimeError, match="injected"):
            apply_plan(settings, session, [mv])
    assert settings.photo_path(mv.rel_to).read_bytes() == original
    with client.session() as session:
        entry = session.scalar(select(MoveLedger))
        assert entry is not None
        assert entry.phase == "quarantine_pending"
        assert session.get(Photo, pid).rel_path == mv.rel_from

    from photo_desk.triage import recover_moves

    with client.session() as session:
        outcomes = recover_moves(settings, session)
        assert len(outcomes) == 1 and outcomes[0].resolved
        assert restore(settings, session, photo_id=pid)[0].restored
    assert settings.photo_path(mv.rel_from).read_bytes() == original


def test_restore_commit_failure_keeps_recoverable_intent(env, monkeypatch):
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    original_path = settings.root / "burst/IMG_0003.jpg"
    original = original_path.read_bytes()
    with client.session() as session:
        report = build_plan(settings, session)
        apply_plan(settings, session, report.moves)
    with client.session() as session:
        commit = session.commit

        def fail_after_move():
            if original_path.exists():
                raise RuntimeError("injected commit failure")
            return commit()

        monkeypatch.setattr(session, "commit", fail_after_move)
        with pytest.raises(RuntimeError, match="injected"):
            restore(settings, session, photo_id=pid)
    assert original_path.read_bytes() == original
    with client.session() as session:
        entry = session.scalar(select(MoveLedger))
        assert entry.phase == "restore_pending"
        assert entry.restored_at is None
        assert session.get(Photo, pid).state == STATE_QUARANTINED

    from photo_desk.triage import recover_moves

    with client.session() as session:
        outcomes = recover_moves(settings, session)
        assert len(outcomes) == 1 and outcomes[0].resolved
        assert session.get(Photo, pid).state == STATE_IN_PLACE
        assert restore(settings, session, photo_id=pid) == []


def test_old_ledger_adds_phase_without_losing_history(tmp_path):
    from sqlalchemy import text

    client = SqliteClient(tmp_path / "old.db")
    try:
        with client.engine.begin() as conn:
            conn.execute(
                text(
                    "CREATE TABLE move_ledger (id INTEGER PRIMARY KEY, "
                    "restored_at DATETIME)"
                )
            )
            conn.execute(text("INSERT INTO move_ledger VALUES (1, NULL)"))
            conn.execute(
                text("INSERT INTO move_ledger VALUES (2, '2026-11-01 12:00:00')")
            )
        ensure_schema(client)
        ensure_schema(client)
        with client.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT id, phase, restored_at FROM move_ledger ORDER BY id")
            ).all()
        assert rows[0] == (1, "quarantined", None)
        assert rows[1] == (2, "quarantined", "2026-11-01 12:00:00")
    finally:
        client.dispose()


def test_legacy_default_phase_respects_restored_at(env):
    settings, client = env
    rows = build_burst(settings, client)
    pid = rows["burst/IMG_0003.jpg"].id
    with client.session() as session:
        apply_plan(settings, session, build_plan(settings, session).moves)
        entry = session.scalar(select(MoveLedger))
        entry.phase = "quarantined"
        session.commit()
        assert restore(settings, session, photo_id=pid)[0].restored
        restored_at = entry.restored_at
        assert restored_at is not None
        entry.phase = "quarantined"
        session.commit()
        assert restore(settings, session, photo_id=pid) == []
        assert entry.restored_at == restored_at
        assert session.get(Photo, pid).state == STATE_IN_PLACE


@pytest.mark.parametrize("restoring", [False, True])
@pytest.mark.parametrize(
    "location", ["source", "destination", "both", "neither", "bad"]
)
def test_recovery_matrix_is_conservative_and_idempotent(env, restoring, location):
    from photo_desk.triage import recover_moves

    settings, client = env
    rows = build_burst(settings, client)
    row0 = rows["burst/IMG_0003.jpg"]
    original = settings.photo_path(row0.rel_path)
    payload = original.read_bytes()
    blurred = settings.blurred_dir / "pending_IMG_0003.jpg"
    blurred.parent.mkdir(parents=True, exist_ok=True)
    with client.session() as session:
        row = session.get(Photo, row0.id)
        entry = MoveLedger(
            photo_id=row.id,
            rel_from=row0.rel_path,
            rel_to="blurred/pending_IMG_0003.jpg",
            sha256=row.content_hash,
            reason="blur_rank",
            phase="restore_pending" if restoring else "quarantine_pending",
        )
        if restoring:
            original.rename(blurred)
            row.rel_path = entry.rel_to
            row.state = STATE_QUARANTINED
        session.add(entry)
        session.commit()
    src, dest = (blurred, original) if restoring else (original, blurred)
    if location == "destination":
        src.rename(dest)
    elif location == "both":
        dest.write_bytes(payload)
    elif location == "neither":
        src.unlink()
    elif location == "bad":
        src.write_bytes(b"changed bytes")
    before = {str(p): p.read_bytes() if p.is_file() else None for p in (src, dest)}
    with client.session() as session:
        outcomes = recover_moves(settings, session)
        assert len(outcomes) == 1
        assert outcomes[0].resolved == (location in ("source", "destination"))
    after = {str(p): p.read_bytes() if p.is_file() else None for p in (src, dest)}
    assert after == before
    with client.session() as session:
        again = recover_moves(settings, session)
        if location in ("source", "destination"):
            assert again == []
            row = session.get(Photo, row0.id)
            in_place = (restoring and location == "destination") or (
                not restoring and location == "source"
            )
            assert row.state == (STATE_IN_PLACE if in_place else STATE_QUARANTINED)
        else:
            assert len(again) == 1 and not again[0].resolved


def test_recovery_refuses_symlink_source(env, monkeypatch) -> None:
    """Windows 无符号链接特权：monkeypatch is_symlink 只对待对账的源路径
    返回 True，其余委托原实现——recover 必须保留 pending、两端字节不动。"""
    from pathlib import Path

    from photo_desk.triage import recover_moves

    settings, client = env
    rows = build_burst(settings, client)
    row0 = rows["burst/IMG_0003.jpg"]
    src = settings.photo_path(row0.rel_path)
    payload = src.read_bytes()
    with client.session() as session:
        row = session.get(Photo, row0.id)
        entry = MoveLedger(
            photo_id=row.id,
            rel_from=row0.rel_path,
            rel_to="blurred/symlink_IMG_0003.jpg",
            sha256=row.content_hash,
            reason="blur_rank",
            phase="quarantine_pending",
        )
        session.add(entry)
        session.commit()
        ledger_id = entry.id
    real_is_symlink = Path.is_symlink

    def fake_is_symlink(self):
        if self == src:
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", fake_is_symlink)
    with client.session() as session:
        outcomes = recover_moves(settings, session)
    assert len(outcomes) == 1
    assert not outcomes[0].resolved and "符号链接" in outcomes[0].detail
    assert src.read_bytes() == payload
    assert not settings.photo_path("blurred/symlink_IMG_0003.jpg").exists()
    with client.session() as session:
        assert session.get(MoveLedger, ledger_id).phase == "quarantine_pending"


def test_pending_blocks_scan_and_triage_without_creating_rows(env):
    settings, client = env
    rows = build_burst(settings, client)
    before = len(rows)
    row0 = rows["burst/IMG_0003.jpg"]
    with client.session() as session:
        session.add(
            MoveLedger(
                photo_id=row0.id,
                rel_from=row0.rel_path,
                rel_to="blurred/pending.jpg",
                sha256=row0.content_hash,
                reason="blur_rank",
                phase="restore_pending",
            )
        )
        session.commit()
    for operation in (scan, build_plan):
        with client.session() as session:
            with pytest.raises(RuntimeError, match="photos recover"):
                operation(settings, session)
            assert len(list(session.scalars(select(Photo)))) == before


@pytest.mark.parametrize("phase", ["quarantine_pending", "restore_pending"])
def test_pending_blocks_apply_and_restore_before_any_move(env, phase) -> None:
    settings, client = env
    rows = build_burst(settings, client)
    before = len(rows)
    row0 = rows["burst/IMG_0003.jpg"]
    other = rows["burst/IMG_0001.jpg"]
    src_bytes = settings.photo_path(row0.rel_path).read_bytes()
    other_bytes = settings.photo_path(other.rel_path).read_bytes()
    with client.session() as session:
        session.add(
            MoveLedger(
                photo_id=row0.id,
                rel_from=row0.rel_path,
                rel_to="blurred/pending.jpg",
                sha256=row0.content_hash,
                reason="blur_rank",
                phase=phase,
            )
        )
        session.commit()
    # build_plan 被 guard 挡住，Move 直接构造：另一张在库、在盘、哈希一致的
    # 合法隔离——guard 若被删掉，这张就真的会被移走。
    mv = Move(
        photo_id=other.id,
        rel_from=other.rel_path,
        rel_to="blurred/guard_IMG_0001.jpg",
        group_id="guardgroup01",
        sharpness=1.0,
        best=2.0,
    )
    with client.session() as session:
        with pytest.raises(RuntimeError, match="photos recover"):
            apply_plan(settings, session, [mv])
        assert len(list(session.scalars(select(Photo)))) == before
        assert len(list(session.scalars(select(MoveLedger)))) == 1
    assert settings.photo_path(other.rel_path).read_bytes() == other_bytes
    assert not settings.photo_path(mv.rel_to).exists()
    with client.session() as session:
        with pytest.raises(RuntimeError, match="photos recover"):
            restore(settings, session, photo_id=row0.id)
        assert len(list(session.scalars(select(MoveLedger)))) == 1
    # pending 意图两端字节不动：源仍在原位，目标从未出现
    assert settings.photo_path(row0.rel_path).read_bytes() == src_bytes
    assert not settings.photo_path("blurred/pending.jpg").exists()


@pytest.mark.parametrize("after_rename", [False, True])
def test_process_exit_retains_intent(env, monkeypatch, after_rename):
    from pathlib import Path

    from photo_desk.triage import recover_moves

    settings, client = env
    build_burst(settings, client)
    with client.session() as session:
        mv = build_plan(settings, session).moves[0]
    original = settings.photo_path(mv.rel_from).read_bytes()
    real_rename = Path.rename

    def interrupted(path, target):
        if after_rename:
            real_rename(path, target)
        raise SystemExit("simulated process exit")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", interrupted)
        with client.session() as session:
            with pytest.raises(SystemExit):
                apply_plan(settings, session, [mv])
    with client.session() as session:
        assert session.scalar(select(MoveLedger)).phase == "quarantine_pending"
        assert recover_moves(settings, session)[0].resolved
        entry = session.scalar(select(MoveLedger))
        assert entry.phase == ("quarantined" if after_rename else "cancelled")
    expected = mv.rel_to if after_rename else mv.rel_from
    assert settings.photo_path(expected).read_bytes() == original
