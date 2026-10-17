"""exam_builder 的 SQLite 存储:ORM 模型 + FTS 索引 + 读写助手。

引擎/PRAGMA/会话策略在通用层 ``storage.sqlite``;FTS 折叠与 MATCH 构造在
``storage.fts``。本模块只声明业务表与"题目域索引哪些列"(同 file_manager 分家)。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from storage import FtsTable, SqliteClient, match_expr, sha256_hex

from . import config


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_path: Mapped[str] = mapped_column(Text)
    filename: Mapped[str] = mapped_column(String(400), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    chapter: Mapped[str] = mapped_column(String(20), default="")
    section: Mapped[str] = mapped_column(String(40), default="")
    lesson: Mapped[str] = mapped_column(String(20), default="")
    title: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")
    error_msg: Mapped[str] = mapped_column(Text, default="")
    ingested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Question(Base):
    __tablename__ = "questions"

    id: Mapped[int] = mapped_column(primary_key=True)
    doc_id: Mapped[int] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    qno: Mapped[int] = mapped_column(Integer)
    ord: Mapped[int] = mapped_column(Integer)
    stem_xml: Mapped[str] = mapped_column(Text)
    answer_xml: Mapped[str] = mapped_column(Text, default="")
    manifest_json: Mapped[str] = mapped_column(Text, default="[]")
    stem_text: Mapped[str] = mapped_column(Text, default="")
    n_figure: Mapped[int] = mapped_column(Integer, default=0)
    n_formula: Mapped[int] = mapped_column(Integer, default=0)


class Paper(Base):
    __tablename__ = "papers"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    question_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    out_path: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


#: 题目域索引的列(写端折叠由 FtsTable 内部完成)
QUESTIONS_INDEX = FtsTable("questions_fts", ("stem_text",))

SCHEMA_VERSION = 1
MIGRATIONS = {1: None}


class Store:
    def __init__(self, db: str | Path | None = None):
        self.client = SqliteClient(db or config.db_path())
        self.client.init_schema(Base.metadata)
        with self.client.session() as session:
            QUESTIONS_INDEX.create(session)
            session.commit()
        self.client.migrate(SCHEMA_VERSION, MIGRATIONS)

    # -- writes -------------------------------------------------------------

    def upsert_document(self, db: Session, **fields) -> Document:
        doc = db.query(Document).filter(Document.filename == fields["filename"]).first()
        if doc is None:
            doc = Document(**fields)
            db.add(doc)
            db.flush()
        else:
            for k, v in fields.items():
                setattr(doc, k, v)
            db.query(Question).filter(Question.doc_id == doc.id).delete()
        return doc

    def add_question(self, db: Session, doc_id: int, **fields) -> Question:
        q = Question(doc_id=doc_id, **fields)
        db.add(q)
        db.flush()
        QUESTIONS_INDEX.upsert(db, q.id, {"stem_text": q.stem_text})
        return q

    # -- reads --------------------------------------------------------------

    def search(
        self,
        db: Session,
        q: str = "",
        chapter: str = "",
        section: str = "",
        lesson: str = "",
        limit: int = 200,
    ) -> list[Question]:
        ids: list[int] | None = None
        if q.strip():
            expr = match_expr(q.split(), prefix=True)
            ids = QUESTIONS_INDEX.rowids_for(db, expr)
            if not ids:
                return []
        query = db.query(Question).join(Document)
        if ids is not None:
            query = query.filter(Question.id.in_(ids))
        if chapter:
            query = query.filter(Document.chapter == chapter)
        if section:
            query = query.filter(Document.section == section)
        if lesson:
            query = query.filter(Document.lesson == lesson)
        return query.order_by(Question.id).limit(limit).all()

    def question(self, db: Session, qid: int) -> Question | None:
        return db.get(Question, qid)

    def manifest(self, q: Question) -> list[dict]:
        return json.loads(q.manifest_json or "[]")

    def dispose(self) -> None:
        self.client.dispose()


def file_sha(path: Path) -> str:
    return sha256_hex(path.read_bytes())
