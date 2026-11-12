"""筛查与放回：全系统唯一的写通道（设计文档 §4.2 / §4.3 / §4.4）。

原件字节永不改写（D-5）——这里只做"移动"，且每次移动都按"提交意图 →
移动 → 提交完成"在 ``move_ledger`` 落行，中断留下可对账的 pending 意图
（``photos recover`` 只补账、不动文件）；放回前先按账本里的 sha256 验身。
**dry-run 是默认**，``--apply`` 是"只看清单"与"真动文件"之间唯一的分界线。

判定是组内相对排名（§4.4）：分数 < 0.5×组内最佳 才隔离，绝不用绝对
阈值——场景光照差足以淹没"模糊"信号。独张照片不参与自动判定；
解码失败的照片没有分数、也就没有结论（宁漏勿错杀）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import quality
from .config import Settings
from .groups import Groupable, group_bursts
from .library import (
    STATE_IN_PLACE,
    STATE_QUARANTINED,
    MoveLedger,
    Photo,
    new_blurred_name,
    require_no_pending_moves,
    utcnow,
)
from .scan import sha256_file

#: 组内相对阈：低于最佳这张比例的判为废片（§4.4，只此一档，不调参）。
RELATIVE_CUT = 0.5


@dataclass
class Move:
    """一次（计划中的）隔离。``rel_to`` 在计划阶段就定死，dry-run 与
    --apply 看到的是同一份清单。"""

    photo_id: int
    rel_from: str
    rel_to: str
    group_id: str
    sharpness: float
    best: float


@dataclass
class TriageReport:
    groups: int = 0
    scored: int = 0
    #: 解码失败、无分数、不参与判定的张数——不是错误，但必须可见
    unscored: int = 0
    moves: list[Move] = field(default_factory=list)


def build_plan(settings: Settings, session: Session) -> TriageReport:
    """连拍归组 → 惰性评分（分数与组 id 写回 photos 表）→ 组内排名。

    只读原件、只写库；不动文件系统。
    """
    settings.ensure_root()
    require_no_pending_moves(session)
    photos = list(
        session.scalars(
            select(Photo).where(Photo.state == STATE_IN_PLACE).order_by(Photo.id)
        )
    )
    by_id = {p.id: p for p in photos}
    items = [
        Groupable(
            photo_id=p.id,
            rel_path=p.rel_path,
            content_hash=p.content_hash,
            taken_at=p.taken_at,
            file_mtime=p.file_mtime_ns / 1e9,
        )
        for p in photos
    ]
    report = TriageReport()
    dirty = False

    for group in group_bursts(items):
        report.groups += 1
        scores: dict[int, float] = {}
        for member in group.members:
            row = by_id[member.photo_id]
            if row.group_id is None:
                row.group_id = group.group_id
                dirty = True
            if row.sharpness is None:
                score = quality.score_file(settings.photo_path(row.rel_path))
                if score is None:
                    report.unscored += 1
                    continue
                row.sharpness = score
                dirty = True
            scores[row.id] = row.sharpness
            report.scored += 1
        if not scores:
            continue
        best = max(scores.values())
        if best <= 0:
            continue  # 全组皆平（纯色/解码退化）：相对排名不成立
        for row_id, score in scores.items():
            if score < RELATIVE_CUT * best:
                row = by_id[row_id]
                report.moves.append(
                    Move(
                        photo_id=row_id,
                        rel_from=row.rel_path,
                        rel_to=new_blurred_name(row.rel_path),
                        group_id=group.group_id,
                        sharpness=score,
                        best=best,
                    )
                )

    if dirty:
        session.commit()
    return report


def _finish_move(entry: MoveLedger, row: Photo, *, restoring: bool) -> None:
    """移动确认后的状态翻转：只改 ORM，不自行提交。"""
    if restoring:
        row.rel_path = entry.rel_from
        row.state = STATE_IN_PLACE
        entry.phase = "restored"
        entry.restored_at = utcnow()
    else:
        row.rel_path = entry.rel_to
        row.state = STATE_QUARANTINED
        entry.phase = "quarantined"
        entry.moved_at = utcnow()
    entry.restore_error = None


def _execute_move(
    settings: Settings,
    session: Session,
    entry: MoveLedger,
    row: Photo,
    *,
    restoring: bool,
) -> None:
    """提交意图 → 移动 → 提交完成。无论中断在哪一步，账本都留下可对账现场。"""
    rel_src, rel_dest = (
        (entry.rel_to, entry.rel_from) if restoring else (entry.rel_from, entry.rel_to)
    )
    src = settings.photo_path(rel_src)
    dest = settings.photo_path(rel_dest)
    if src.is_symlink() or not src.is_file():
        raise RuntimeError(f"源文件不可移动: {rel_src}")
    if dest.exists() or dest.is_symlink():
        raise RuntimeError(f"目标已被占用，拒绝覆盖: {rel_dest}")
    if sha256_file(src) != entry.sha256:
        raise RuntimeError(f"sha256 不符，拒绝移动: {rel_src}")
    entry.phase = "restore_pending" if restoring else "quarantine_pending"
    session.add(entry)
    session.commit()
    try:
        # 单维护写者前提下再检查一次；不宣称可抵抗外部并发写入。
        if dest.exists() or dest.is_symlink():
            raise RuntimeError(f"目标已被占用，拒绝覆盖: {rel_dest}")
        src.rename(dest)
        _finish_move(entry, row, restoring=restoring)
        session.commit()
    except Exception:
        session.rollback()
        raise


def apply_plan(settings: Settings, session: Session, moves: list[Move]) -> int:
    """执行隔离：意图先落盘 → rename 进 blurred/ → 账本确认 + 状态翻转。

    ``Photo.rel_path`` 始终跟**当前**位置（详情/缩略图靠它找文件），
    原始位置保存在账本 ``rel_from`` 里供放回。
    """
    settings.ensure_root()
    require_no_pending_moves(session)
    settings.blurred_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for mv in moves:
        row = session.get(Photo, mv.photo_id)
        if row is None or row.state != STATE_IN_PLACE or row.rel_path != mv.rel_from:
            print(f"跳过 {mv.rel_from}：库状态已变，计划过期")
            continue
        src = settings.photo_path(mv.rel_from)
        if not src.is_file():
            print(f"跳过 {mv.rel_from}：目录里没有这个文件，请先 photos scan")
            continue
        entry = MoveLedger(
            photo_id=row.id,
            rel_from=mv.rel_from,
            rel_to=mv.rel_to,
            sha256=row.content_hash,
            reason="blur_rank",
            group_id=mv.group_id,
        )
        _execute_move(settings, session, entry, row, restoring=False)
        moved += 1
    return moved


@dataclass
class RestoreOutcome:
    photo_id: int
    rel_in_blurred: str
    restored: bool
    detail: str


def restore(
    settings: Settings,
    session: Session,
    *,
    photo_id: int | None = None,
    group_id: str | None = None,
) -> list[RestoreOutcome]:
    """放回（单张或整组）。每一步都以账本行为准，验身不过就**留在** blurred。

    - sha256 与账本不符（有人在隔离区动过字节）→ 记 ``restore_error``，不放回；
    - 原父目录已被删 → 重建后放回，``note='recreated_parent'``（孤儿可救）；
    - 原位置已被别的文件占用 → 拒绝覆盖，绝不销毁未知数据。

    业务拒绝仍返回 outcome；真正的 I/O/commit 故障中止本批并保留 pending
    意图，前面已完成的照片不回滚伪装成全批失败未发生。
    """
    settings.ensure_root()
    if photo_id is None and group_id is None:
        raise ValueError("restore 需要指定 photo_id 或 group_id")
    require_no_pending_moves(session)
    stmt = select(MoveLedger).where(
        MoveLedger.restored_at.is_(None), MoveLedger.phase == "quarantined"
    )
    if photo_id is not None:
        stmt = stmt.where(MoveLedger.photo_id == photo_id)
    if group_id is not None:
        stmt = stmt.where(MoveLedger.group_id == group_id)
    entries = list(session.scalars(stmt.order_by(MoveLedger.id)))
    outcomes: list[RestoreOutcome] = []
    for entry in entries:
        cur = settings.photo_path(entry.rel_to)
        dest = settings.photo_path(entry.rel_from)
        row = session.get(Photo, entry.photo_id)
        outcome = RestoreOutcome(entry.photo_id, entry.rel_to, False, "")
        outcomes.append(outcome)
        error = None
        if cur.is_symlink() or not cur.is_file():
            error = "blurred 区里找不到可用的普通文件"
        elif sha256_file(cur) != entry.sha256:
            error = "sha256 不符（内容被改过）"
        elif dest.exists() or dest.is_symlink():
            error = "原位置已被占用，拒绝覆盖"
        elif row is None or row.rel_path != entry.rel_to:
            error = "照片记录与账本不符，拒绝放回"
        elif row.content_hash != entry.sha256 or row.state != STATE_QUARANTINED:
            error = "照片身份或状态与账本不符，拒绝放回"
        if error is not None:
            entry.restore_error = error
            outcome.detail = error
            session.commit()
            continue
        assert row is not None
        if not dest.parent.is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            entry.note = "recreated_parent"
        _execute_move(settings, session, entry, row, restoring=True)
        outcome.restored = True
        suffix = f"（{entry.note}）" if entry.note else ""
        outcome.detail = "放回 " + entry.rel_from + suffix
    return outcomes


@dataclass
class RecoveryOutcome:
    ledger_id: int
    resolved: bool
    detail: str


def recover_moves(settings: Settings, session: Session) -> list[RecoveryOutcome]:
    """对账中断的移动：只核验两端并补记数据库，绝不移动或覆盖文件。

    结论只基于账本已记录的两个路径，不遍历磁盘猜原件。歧义现场（两端
    同在/同缺、哈希不符、符号链接、Photo 身份不符）保留 pending、两端
    字节不动，交人工核定；对账时间是确认时间，不冒充精确 rename 时刻。
    """
    settings.ensure_root()
    entries = list(
        session.scalars(
            select(MoveLedger)
            .where(MoveLedger.phase.in_(("quarantine_pending", "restore_pending")))
            .order_by(MoveLedger.id)
        )
    )
    outcomes: list[RecoveryOutcome] = []
    for entry in entries:
        restoring = entry.phase == "restore_pending"
        rel_src, rel_dest = (
            (entry.rel_to, entry.rel_from)
            if restoring
            else (entry.rel_from, entry.rel_to)
        )
        src = settings.photo_path(rel_src)
        dest = settings.photo_path(rel_dest)
        try:
            row = session.get(Photo, entry.photo_id)
            if (
                row is None
                or row.content_hash != entry.sha256
                or row.rel_path not in (entry.rel_from, entry.rel_to)
            ):
                raise ValueError("Photo 身份/路径与账本不符")
            if src.is_symlink() or dest.is_symlink():
                raise ValueError("路径含符号链接，需人工核对")
            src_exists, dest_exists = src.exists(), dest.exists()
            if src_exists == dest_exists:
                raise ValueError("两端同时存在或同时缺失，需人工核对")
            actual = src if src_exists else dest
            if not actual.is_file() or sha256_file(actual) != entry.sha256:
                raise ValueError("实际文件 sha256 不符或不是普通文件")
        except (OSError, ValueError) as exc:
            outcomes.append(RecoveryOutcome(entry.id, False, str(exc)))
            continue
        if dest_exists:
            _finish_move(entry, row, restoring=restoring)
            detail = "按文件现状补记移动完成"
        else:
            entry.phase = "quarantined" if restoring else "cancelled"
            row.rel_path = rel_src
            row.state = STATE_QUARANTINED if restoring else STATE_IN_PLACE
            entry.restore_error = None
            detail = "移动未发生，已撤销意图；未移动任何文件"
        session.commit()
        outcomes.append(RecoveryOutcome(entry.id, True, detail))
    return outcomes


def pending_quarantined(session: Session) -> list[Photo]:
    """当前躺在隔离区的照片（时间线灰显与 M2 编辑模式的数据源）。"""
    return list(
        session.scalars(
            select(Photo)
            .where(Photo.state == STATE_QUARANTINED)
            .order_by(Photo.id.desc())
        )
    )
