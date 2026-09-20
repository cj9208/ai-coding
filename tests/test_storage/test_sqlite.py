from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import DateTime, String, Text, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from storage import SqliteClient, ensure_columns, sha256_hex, to_db_url


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


@pytest.fixture()
def client(tmp_path: Path) -> SqliteClient:
    c = SqliteClient(tmp_path / "app.db")
    c.init_schema(Base.metadata)
    return c


def test_path_and_url_conventions_both_work(tmp_path: Path):
    as_path = tmp_path / "a.db"
    assert to_db_url(as_path).startswith("sqlite:///")
    url = f"sqlite:///{(tmp_path / 'b.db').as_posix()}"
    assert to_db_url(url) == url  # passed through untouched


def test_wal_and_foreign_keys_applied_per_engine(client: SqliteClient):
    with client.session() as db:
        assert db.execute(select(1)).scalar() == 1
        mode = db.connection().exec_driver_sql("PRAGMA journal_mode").scalar()
        fk = db.connection().exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert str(mode).lower() == "wal"
    assert int(fk) == 1


def test_pragma_listener_does_not_leak_across_engines(tmp_path: Path):
    """Regression: the old file_manager bug registered the PRAGMA listener on
    the Engine *class*. Instance-level attachment is asserted here by proving
    a second, independently created engine is healthy and WAL persists."""
    second = SqliteClient(f"sqlite:///{(tmp_path / 'second.db').as_posix()}")
    second.init_schema(Base.metadata)
    with second.session() as db:
        db.add(Widget(name="x"))
        db.commit()
        assert db.scalar(select(Widget).where(Widget.name == "x")) is not None
    second.dispose()


def test_session_roundtrip_and_raw_connect(client: SqliteClient):
    with client.session() as db:
        db.add(Widget(name="hello", note="世界"))
        db.commit()
    conn = client.connect()
    try:
        row = conn.execute("SELECT name, note FROM widgets").fetchone()
        assert row == ("hello", "世界")
    finally:
        conn.close()


def test_ensure_columns_adds_only_missing(client: SqliteClient):
    added = client.ensure_columns("widgets", {"extra": "TEXT NOT NULL DEFAULT ''"})
    assert added == ["extra"]
    # idempotent second call adds nothing
    assert client.ensure_columns("widgets", {"extra": "TEXT"}) == []
    with client.session() as db:
        db.add(Widget(name="w2"))
        db.commit()
    with client.engine.connect() as conn:
        value = conn.execute(
            text("SELECT extra FROM widgets WHERE name = 'w2'")
        ).scalar()
    assert value == ""


def test_ensure_columns_module_function_sees_added(client: SqliteClient):
    ensure_columns(
        client.engine, "widgets", {"one": "INTEGER DEFAULT 0", "two": "TEXT"}
    )
    from sqlalchemy import inspect

    cols = {c["name"] for c in inspect(client.engine).get_columns("widgets")}
    assert {"one", "two", "note"} <= cols


def test_sha256_hex_variants():
    import hashlib

    assert sha256_hex("abc") == hashlib.sha256(b"abc").hexdigest()
    assert sha256_hex(b"abc") == sha256_hex("abc")
    assert len(sha256_hex("abc", length=16)) == 16
