"""数据表与轻量查询。

M0 只有 ``photo`` 表；``tag``（M2）与 ``move_ledger``（M1）按设计文档 §4
在各自里程碑加入。**所有路径列存相对 PHOTO_ROOT 的 POSIX 字符串**（§4.1），
任何消费方需要绝对路径时经 ``Settings.photo_path`` 现拼。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import BigInteger, DateTime, Index, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

#: 照片在库状态。``quarantined`` 由 M1 隔离账本翻转。
STATE_IN_PLACE = "in_place"
STATE_MISSING = "missing"
STATE_QUARANTINED = "quarantined"
STATE_ORPHANED = "orphaned"


def new_blurred_name(original_rel: str) -> str:
    """隔离区落盘名：blurred/<uuid12>_<干><扩展名>。uuid 前缀避免同名冲突，
    保留原名便于人翻。"""
    from .config import BLURRED_DIR_NAME

    tail = original_rel.rsplit("/", 1)[-1]
    stem, _, ext = tail.rpartition(".")
    return (
        f"{BLURRED_DIR_NAME}/{uuid.uuid4().hex[:12]}_{stem}.{ext.lower()}"
        if ext
        else f"{BLURRED_DIR_NAME}/{uuid.uuid4().hex[:12]}_{stem}"
    )


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Photo(Base):
    __tablename__ = "photos"
    __table_args__ = (Index("ix_photos_taken_at", "taken_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    #: 相对 PHOTO_ROOT 的 POSIX 路径，如 "2025/春/IMG_0001.jpg"
    rel_path: Mapped[str] = mapped_column(String(600), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    file_mtime_ns: Mapped[int] = mapped_column(BigInteger)
    #: EXIF DateTimeOriginal（+SubSecTime 毫秒），naive 本地拍摄时刻；无 EXIF 为 None
    taken_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    gps_lat: Mapped[float | None] = mapped_column(nullable=True)
    gps_lng: Mapped[float | None] = mapped_column(nullable=True)
    make: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    width: Mapped[int | None] = mapped_column(nullable=True)
    height: Mapped[int | None] = mapped_column(nullable=True)
    extension: Mapped[str] = mapped_column(String(10))
    state: Mapped[str] = mapped_column(String(20), default=STATE_IN_PLACE)
    #: 清晰度分（拉普拉斯方差）。None = 还没评过分。由 triage 惰性写入。
    sharpness: Mapped[float | None] = mapped_column(nullable=True)
    #: 所属连拍组（首张内容哈希前 12 位），None = 未归组/独张。
    group_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    @property
    def effective_taken(self) -> datetime:
        """时间线分组用：EXIF 拍摄时刻优先，缺 EXIF（截图/PNG）回退文件 mtime。"""
        if self.taken_at is not None:
            return self.taken_at
        return datetime.fromtimestamp(self.file_mtime_ns / 1e9)

    @property
    def day(self):  # noqa: ANN401 - date，避免为属性引入额外 import 歧义
        return self.effective_taken.date()


#: SQL 侧与 ``effective_taken`` 同语义的排序表达式（EXIF 优先、mtime 兜底）。
effective_taken_expr = func.coalesce(
    Photo.taken_at,
    func.datetime(Photo.file_mtime_ns / 1_000_000_000, "unixepoch", "localtime"),
)


def iter_photos(
    session: Session, *, state: str | None = STATE_IN_PLACE
) -> Iterator[Photo]:
    """按拍摄时间倒序给出照片（时间线数据源）。``state=None`` 表示不过滤状态。"""
    stmt = select(Photo).order_by(effective_taken_expr.desc(), Photo.id.desc())
    if state is not None:
        stmt = stmt.where(Photo.state == state)
    for photo in session.scalars(stmt):
        yield photo


class MoveLedger(Base):
    """隔离账本（设计文档 §4.2）：一次移动一行，**永不删行**——放回是
    ``restored_at`` 置位，操作历史本身即审计面。``note`` 记非致命异常
    （如放回时重建了已被删掉的父目录）。"""

    __tablename__ = "move_ledger"

    id: Mapped[int] = mapped_column(primary_key=True)
    photo_id: Mapped[int] = mapped_column(index=True)
    #: 均为相对 PHOTO_ROOT 的 POSIX 路径
    rel_from: Mapped[str] = mapped_column(String(600))
    rel_to: Mapped[str] = mapped_column(String(600))
    #: 移动当时的内容指纹——放回前先验身
    sha256: Mapped[str] = mapped_column(String(64))
    #: blur_rank（组内排名垫底）/ manual（编辑模式人手，M2）
    reason: Mapped[str] = mapped_column(String(20))
    group_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    moved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    restored_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    restore_error: Mapped[str | None] = mapped_column(String(200), nullable=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)


#: photos 表 M1 新增列——已建库用 ensure_columns 轻量补（storage 姿势，不上迁移框架）。
_M1_PHOTO_ADDITIONS = {"sharpness": "FLOAT", "group_id": "VARCHAR(12)"}


def ensure_schema(storage) -> None:  # noqa: ANN001 - storage.SqliteClient
    """建表 + 增量补列。app/CLI 启动统一走这里，别各自 create_all。"""
    storage.init_schema(Base.metadata)
    storage.ensure_columns("photos", _M1_PHOTO_ADDITIONS)
