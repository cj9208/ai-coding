"""exam_builder 切题/投影/分类的单测 + golden 夹具回归。

golden 夹具(tests/golden/exam_split.jsonl)锁的是*合成* fixture 而非用户的
19MB 真实语料:CI 必须可复现,真实 docx 不进仓库。真实语料的验收记录在
docs/exam-builder-design.md §8。
"""

from __future__ import annotations

import json
from pathlib import Path

from lxml import etree

from exam_builder import parse

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN = Path(__file__).parents[1] / "golden" / "exam_split.jsonl"


def _root(name: str):
    return etree.fromstring((FIXTURES / name).read_bytes())


def _golden():
    for line in GOLDEN.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def test_golden_split():
    for case in _golden():
        res = parse.split(parse.body_nodes(_root(case["file"])))
        assert [b.qno for b in res.questions] == case["questions"], case["file"]
        assert [b.qno for b in res.answers] == case["answers"], case["file"]
        firsts = [parse.stem_text(b.nodes).splitlines()[0] for b in res.questions]
        assert firsts == case["first_lines"], case["file"]
        for err in case["errors"]:
            assert any(err in e for e in res.errors), (case["file"], res.errors)


def test_subparts_stay_in_parent_question():
    res = parse.split(parse.body_nodes(_root("doc_basic.xml")))
    q3 = [b for b in res.questions if b.qno == 3][0]
    assert len(q3.nodes) == 2  # （1）（2）不新开一题
    assert "（2）" in q3.text


def test_stem_text_placeholders():
    res = parse.split(parse.body_nodes(_root("doc_basic.xml")))
    q1 = parse.stem_text(res.questions[0].nodes)
    q2 = parse.stem_text(res.questions[1].nodes)
    assert parse.FORMULA_PLACEHOLDER in q1
    assert parse.FIGURE_PLACEHOLDER in q2


def test_manifest_roles_and_relids():
    root = _root("doc_basic.xml")
    rels = {"rId9": "word/embeddings/oleObject1.bin", "rId8": "word/media/image1.wmf"}
    res = parse.split(parse.body_nodes(root))
    m = {p.role: p for p in parse.manifest_for(res.questions[0].nodes, rels)}
    assert m["ole"].part.endswith(".bin")
    m2 = {p.role: p for p in parse.manifest_for(res.questions[1].nodes, rels)}
    assert m2["formula_preview"].part.endswith(".wmf")


def test_classify_part():
    assert parse.classify_part("word/embeddings/oleObject1.bin") == "ole"
    assert parse.classify_part("word/media/image1.wmf") == "formula_preview"
    assert parse.classify_part("word/media/image1.png") == "figure"


def test_serialize_roundtrip_preserves_object():
    res = parse.split(parse.body_nodes(_root("doc_basic.xml")))
    xml = parse.serialize(res.questions[0].nodes)
    back = etree.fromstring(f"<wrap xmlns:w='{parse.W}'>{xml}</wrap>")
    assert back.find(f".//{parse.TAG_OBJECT}") is not None


def test_parse_filename():
    t = parse.parse_filename("13.2.1 三角形的边（第2课时）.docx")
    assert (t["chapter"], t["section"], t["lesson"]) == ("13", "13.2.1", "第2课时")
    assert t["title"] == "三角形的边"
    t = parse.parse_filename("第十七章 小结.docx")
    assert t["chapter"] == "十七" and t["title"] == "小结"


def test_missing_anchor_is_error_not_guess():
    res = parse.split(parse.body_nodes(_root("doc_noanchor.xml")))
    assert res.questions == []
    assert res.errors
