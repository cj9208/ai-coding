from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from storage import FtsTable, SqliteClient, fold_cjk, match_expr, token_expr


class Base(DeclarativeBase):
    pass


class Doc(Base):
    __tablename__ = "docs"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(200), default="")
    body: Mapped[str] = mapped_column(String(2000), default="")


INDEX = FtsTable("docs_fts", ("title", "body"))


@pytest.fixture()
def client(tmp_path: Path) -> SqliteClient:
    c = SqliteClient(tmp_path / "fts.db")
    c.init_schema(Base.metadata)
    with c.session() as db:
        INDEX.create(db)
        db.commit()
    return c


def _insert(client: SqliteClient, doc_id: int, title: str, body: str) -> None:
    with client.session() as db:
        db.add(Doc(id=doc_id, title=title, body=body))
        INDEX.upsert(db, doc_id, {"title": title, "body": body})
        db.commit()


def test_fold_cjk_windows():
    folded = fold_cjk("2025年财报")
    # bigram present, single chars present, ascii untouched
    assert "财报" in folded.split()
    assert "2025年" not in folded.split()  # digits+CJK boundary splits
    assert fold_cjk(None) == ""
    assert fold_cjk("plain ascii") == "plain ascii"


def test_match_expr_parenthesises_each_token():
    expr = match_expr(["财报", "risk"])
    assert expr.startswith("(") and ") AND (" in expr
    assert match_expr([]) == ""


def test_prefix_wildcard_only_on_ascii_units():
    assert token_expr("repo", prefix=True) == "(repo*)"
    assert token_expr("财报", prefix=True) == "(财 AND 报 AND 财报)"  # CJK exact
    assert token_expr("repo") == "(repo)"  # opt-in only
    assert match_expr(["年报"], joiner=" OR ", prefix=True) == "(年 AND 报 AND 年报)"


def test_cjk_bigram_hit_and_no_stray_char_hit(client: SqliteClient):
    _insert(client, 1, " quarterly 报告", "本季度营收增长")
    _insert(client, 2, "笔记", "只提到报和导两个散字")
    with client.session() as db:
        # "报告" must match doc 1 only: doc 2 has 报 and 告 as scattered chars,
        # the folded bigram 报告 is absent -> no false positive.
        hits = INDEX.rowids_for(db, match_expr(["报告"]))
        assert hits == [1]
        # prefix-style query still works on the folded unigrams
        assert set(INDEX.rowids_for(db, match_expr(["营收"]))) == {1}


def test_upsert_replaces_and_delete_removes(client: SqliteClient):
    _insert(client, 7, "旧标题", "旧内容")
    with client.session() as db:
        INDEX.upsert(db, 7, {"title": "新标题", "body": "换了内容"})
        db.commit()
    with client.session() as db:
        assert INDEX.rowids_for(db, match_expr(["新标题"])) == [7]
        assert INDEX.rowids_for(db, match_expr(["旧标题"])) == []
        INDEX.delete(db, 7)
        db.commit()
    with client.session() as db:
        assert INDEX.rowids_for(db, match_expr(["换了"])) == []


def test_unknown_column_rejected(client: SqliteClient):
    with client.session() as db, pytest.raises(KeyError):
        INDEX.upsert(db, 1, {"nope": "x"})


def test_orm_and_index_share_one_session_transaction(client: SqliteClient):
    _insert(client, 9, "final", "收尾测试")
    with client.session() as db:
        row = db.scalar(select(Doc).where(Doc.id == 9))
        assert row is not None and row.title == "final"
        assert INDEX.rowids_for(db, match_expr(["final"])) == [9]
