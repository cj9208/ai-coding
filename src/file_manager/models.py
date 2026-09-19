from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Team(Base):
    __tablename__ = "teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    members: Mapped[list[Member]] = relationship(back_populates="team")


class Member(Base):
    __tablename__ = "members"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    # 允许暂不归属团队（如首次上传自动注册的用户），团队视图只统计有归属的文件
    team_id: Mapped[int | None] = mapped_column(
        ForeignKey("teams.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    team: Mapped[Team | None] = relationship(back_populates="members")


class Project(Base):
    __tablename__ = "projects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    files: Mapped[list[FileMeta]] = relationship(back_populates="project")


class FileMeta(Base):
    __tablename__ = "files"
    __table_args__ = (
        UniqueConstraint("project_id", "content_hash", name="uq_files_project_hash"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    original_filename: Mapped[str] = mapped_column(String(500))
    # 相对 files_dir 的路径：<project_id>/<uuid>_<文件名>
    rel_path: Mapped[str] = mapped_column(String(600), unique=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    size: Mapped[int] = mapped_column(BigInteger)
    extension: Mapped[str | None] = mapped_column(String(20), nullable=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"))
    # 上传者与团队都留快照字段：人员换团队、甚至从花名册删除后，历史文件仍可追溯
    uploader_id: Mapped[int | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True
    )
    uploader_name: Mapped[str] = mapped_column(String(200), default="")
    team_id: Mapped[int | None] = mapped_column(nullable=True)  # 快照，刻意不设外键
    team_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    title: Mapped[str | None] = mapped_column(String(500), nullable=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[str] = mapped_column(String(500), default="")  # 逗号分隔
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    project: Mapped[Project] = relationship(back_populates="files")

    @property
    def project_name(self) -> str:
        return self.project.name
