"""数据表与轻量查询。

M0 只有 ``photo`` 表；``tag``（M2）与 ``move_ledger``（M1）按设计文档 §4
在各自里程碑加入。**所有路径列存相对 PHOTO_ROOT 的 POSIX 字符串**（§4.1），
任何消费方需要绝对路径时经 ``Settings.photo_path`` 现拼。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterator

from sqlalchemy import BigInteger, DateTime, Index, String, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

#: 照片在库状态。``quarantined`` 由 M1 隔离账本翻转，M0 只产生前两种。
STATE_IN_PLACE = "in_place"
STATE_MISSING = "missing"
STATE_QUARANTINED = "quarantined"
STATE_ORPHANED = "orphaned"


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
