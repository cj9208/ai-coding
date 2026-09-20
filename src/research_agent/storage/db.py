"""SQLAlchemy tables mirroring docs 05 §4. Append-only except the session row.

JSON-vs-column rule from the doc: artifacts stay JSON blobs (only kind +
schema_version are queried); anything the *machine* filters on — dedup
hashes, gap status, budget counters — gets real columns.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Index, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    phase: Mapped[str] = mapped_column(String, default="INTAKE")
    stop_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)
    updated_at: Mapped[str] = mapped_column(String, default=utcnow)


class ArtifactRow(Base):
    __tablename__ = "artifacts"

    seq: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)
    payload: Mapped[str] = mapped_column(Text)  # JSON
    schema_version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(String, default=utcnow)


class CaptureRow(Base):
    __tablename__ = "captures"

    # ids restart per session ("C1" in two sessions is fine) -> composite PK
    id: Mapped[str] = mapped_column(String, primary_key=True)
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    url: Mapped[str] = mapped_column(String)
    content_hash: Mapped[str] = mapped_column(String)
    fetched_at: Mapped[str] = mapped_column(String, default=utcnow)
    text: Mapped[str] = mapped_column(Text)
    adapter: Mapped[str] = mapped_column(String, default="")
    fetch_status: Mapped[str] = mapped_column(String, default="ok")
    title: Mapped[str] = mapped_column(String, default="")
    # UNIQUE(session, url, content_hash) gives dedup + budget protection (05 §4)
    __table_args__ = (
        Index("uq_capture", "session_id", "url", "content_hash", unique=True),
    )


class FindingRow(Base):
    __tablename__ = "findings"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # "F1", cited in reports
    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    capture_id: Mapped[str] = mapped_column(String, index=True)
    claim: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String, default="fact")
    decision_criteria: Mapped[str] = mapped_column(Text, default="")  # JSON list
    touches_candidates: Mapped[str] = mapped_column(Text, default="")  # JSON list
    quote: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(default=0.6)
    support_key: Mapped[str] = mapped_column(String, index=True, default="")
    batch: Mapped[int] = mapped_column(
        default=0
    )  # collection round, for saturation heuristics


class GapEventRow(Base):
    """Open/resolved folds stay auditable: which batch closed an evidence gap,
    which answer closed a preference gap (05 §4)."""

    __tablename__ = "gap_events"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, index=True)
    kind: Mapped[str] = mapped_column(String)  # evidence | preference
    gap_id: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String)  # open | resolved | asked
    ref: Mapped[str] = mapped_column(String, default="")  # batch / question id


class BudgetCounterRow(Base):
    __tablename__ = "budget_counters"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, primary_key=True)
    used: Mapped[int] = mapped_column(default=0)
    max: Mapped[int] = mapped_column(default=0)


class ReportRow(Base):
    __tablename__ = "reports"

    session_id: Mapped[str] = mapped_column(String, primary_key=True)
    path: Mapped[str] = mapped_column(String)
    rendered_at: Mapped[str] = mapped_column(String, default=utcnow)


def make_engine(db_url: str) -> Engine:
    engine = create_engine(
        db_url,
        connect_args=(
            {"check_same_thread": False} if db_url.startswith("sqlite") else {}
        ),
    )

    @event.listens_for(engine, "connect")
    def _pragma(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def init_db(engine: Engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
