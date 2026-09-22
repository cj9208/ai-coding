"""筛查与放回：全系统唯一的写通道（设计文档 §4.2 / §4.3 / §4.4）。

原件字节永不改写（D-5）——这里只做"移动"，且每次移动必须在
``move_ledger`` 落一行；放回前先按账本里的 sha256 验身。**dry-run 是默认**，
``--apply`` 是"只看清单"与"真动文件"之间唯一的分界线。

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


def apply_plan(settings: Settings, session: Session, moves: list[Move]) -> int:
    """执行隔离：rename 进 blurred/ + 账本落行 + 状态翻转。

    ``Photo.rel_path`` 始终跟**当前**位置（详情/缩略图靠它找文件），
    原始位置保存在账本 ``rel_from`` 里供放回。
    """
    settings.ensure_root()
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
        dest = settings.photo_path(mv.rel_to)
        src.rename(dest)
        session.add(
            MoveLedger(
                photo_id=row.id,
                rel_from=mv.rel_from,
                rel_to=mv.rel_to,
                sha256=row.content_hash,
                reason="blur_rank",
                group_id=mv.group_id,
            )
        )
        row.rel_path = mv.rel_to
        row.state = STATE_QUARANTINED
        moved += 1
    if moved:
        session.commit()
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
    """
    settings.ensure_root()
    if photo_id is None and group_id is None:
        raise ValueError("restore 需要指定 photo_id 或 group_id")

    stmt = select(MoveLedger).where(MoveLedger.restored_at.is_(None))
    if photo_id is not None:
        stmt = stmt.where(MoveLedger.photo_id == photo_id)
    if group_id is not None:
        stmt = stmt.where(MoveLedger.group_id == group_id)
    entries = list(session.scalars(stmt.order_by(MoveLedger.id)))

    outcomes: list[RestoreOutcome] = []
    did_work = False
    for entry in entries:
        cur = settings.photo_path(entry.rel_to)
        outcome = RestoreOutcome(
            photo_id=entry.photo_id,
            rel_in_blurred=entry.rel_to,
            restored=False,
            detail="",
        )
        outcomes.append(outcome)

        if not cur.is_file():
            entry.restore_error = "blurred 区里找不到该文件"
            outcome.detail = entry.restore_error
            did_work = True
            continue
        digest = sha256_file(cur)
        if digest != entry.sha256:
            entry.restore_error = f"sha256 不符（内容被改过）: {digest[:12]}…"
            outcome.detail = entry.restore_error
            did_work = True
            continue
        dest = settings.photo_path(entry.rel_from)
        if dest.exists():
            entry.restore_error = "原位置已被占用，拒绝覆盖"
            outcome.detail = entry.restore_error
            did_work = True
            continue

        note: str | None = None
        if not dest.parent.is_dir():
            dest.parent.mkdir(parents=True, exist_ok=True)
            note = "recreated_parent"
        cur.rename(dest)
        entry.restored_at = utcnow()
        entry.note = note
        row = session.get(Photo, entry.photo_id)
        if row is not None:
            row.rel_path = entry.rel_from
            row.state = STATE_IN_PLACE
        outcome.restored = True
        outcome.detail = "放回 " + entry.rel_from + (f"（{note}）" if note else "")
        did_work = True

    if did_work:
        session.commit()
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
