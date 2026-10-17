"""store 的 FTS(CJK 折叠)+ 章节筛选回归。"""

from __future__ import annotations

from exam_builder.store import Question, Store


def _seed(tmp_path, stem: str, chapter: str = "13") -> Store:
    store = Store(tmp_path / "t.db")
    with store.client.session() as db:
        doc = store.upsert_document(
            db,
            filename="x.docx",
            source_path="/x.docx",
            sha256="0" * 64,
            chapter=chapter,
            section=chapter,
            lesson="",
            title="t",
            status="ok",
            error_msg="",
        )
        store.add_question(
            db,
            doc.id,
            qno=1,
            ord=0,
            stem_xml="<p/>",
            answer_xml="",
            manifest_json="[]",
            stem_text=stem,
            n_figure=0,
            n_formula=0,
        )
        db.commit()
    return store


def test_cjk_search_hits_bigram(tmp_path):
    store = _seed(tmp_path, "3．如果等腰三角形的一边长是5，求它的周长．")
    with store.client.session() as db:
        hits = store.search(db, q="等腰")
        assert len(hits) == 1
        assert hits[0].stem_text.startswith("3．")
        assert store.search(db, q="平行四边形") == []
    store.dispose()


def test_chapter_filter_combines_with_fts(tmp_path):
    store = _seed(tmp_path, "等腰三角形的周长", chapter="15")
    with store.client.session() as db:
        assert len(store.search(db, q="等腰", chapter="15")) == 1
        assert store.search(db, q="等腰", chapter="13") == []
    store.dispose()


def test_upsert_same_filename_replaces_questions(tmp_path):
    store = _seed(tmp_path, "旧题干")
    with store.client.session() as db:
        doc = store.upsert_document(
            db,
            filename="x.docx",
            source_path="/x.docx",
            sha256="1" * 64,
            chapter="13",
            section="13",
            lesson="",
            title="t",
            status="ok",
            error_msg="",
        )
        store.add_question(
            db,
            doc.id,
            qno=1,
            ord=0,
            stem_xml="<p/>",
            answer_xml="",
            manifest_json="[]",
            stem_text="新题干",
            n_figure=0,
            n_formula=0,
        )
        db.commit()
    with store.client.session() as db:
        assert db.query(Question).count() == 1
        assert store.search(db, q="旧题") == []
        assert len(store.search(db, q="新题")) == 1
    store.dispose()
