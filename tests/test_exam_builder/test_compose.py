"""compose 包级手术的往返/完整性测试(设计 §6.3 验收)。

不依赖用户语料:用 fixture 现场造一个最小 docx(含 OLE 公式 + WMF 预览),
ingest 后 compose,再断言新包里 关系可解析、部件齐全、题干文本在、题号重排。
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from lxml import etree

from exam_builder import compose as compose_mod
from exam_builder import ingest, parse
from exam_builder.store import Question, Store

FIXTURES = Path(__file__).parent / "fixtures"

_IMG = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_OLE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject"
_OFFICE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _rel(rid: str, rtype: str, target: str) -> str:
    return f'<Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'


_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/'
    '2006/relationships">\n'
    + _rel("rId8", _IMG, "media/image1.wmf")
    + _rel("rId9", _OLE, "embeddings/oleObject1.bin")
    + "</Relationships>"
)


def _ct_default(ext: str, ctype: str) -> str:
    return f'<Default Extension="{ext}" ContentType="{ctype}"/>'


def _ct_override(part: str, ctype: str) -> str:
    return f'<Override PartName="{part}" ContentType="{ctype}"/>'


_CT = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
    'content-types">\n'
    + _ct_default("rels", "application/vnd.openxmlformats-package.relationships+xml")
    + _ct_default("xml", "application/xml")
    + _ct_default("wmf", "image/x-wmf")
    + _ct_default("bin", "application/vnd.openxmlformats-officedocument.oleObject")
    + _ct_override(
        "/word/document.xml",
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document.main+xml",
    )
    + "</Types>"
)


def _make_docx(dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w") as zf:
        zf.writestr("[Content_Types].xml", _CT)
        zf.writestr(
            "_rels/.rels",
            '<?xml version="1.0"?><Relationships xmlns="http://schemas.'
            'openxmlformats.org/package/2006/relationships">'
            + _rel("rId1", _OFFICE + "/officeDocument", "word/document.xml")
            + "</Relationships>",
        )
        zf.writestr("word/document.xml", (FIXTURES / "doc_basic.xml").read_bytes())
        zf.writestr("word/_rels/document.xml.rels", _RELS)
        zf.writestr("word/media/image1.wmf", b"WMFDUMMY")
        zf.writestr("word/embeddings/oleObject1.bin", b"OLEDUMMY")
    return dest


def test_compose_roundtrip(tmp_path, monkeypatch):
    from exam_builder import config

    monkeypatch.setattr(
        config, "media_path", lambda sha, ext: tmp_path / "media" / f"{sha}.{ext}"
    )
    src = _make_docx(tmp_path / "src.docx")
    store = Store(tmp_path / "data" / "t.db")
    rep = ingest.ingest_file(store, src)
    assert rep.status == "ok" and rep.n_questions == 3

    with store.client.session() as db:
        ids = [q.id for q in db.query(Question).order_by(Question.id).all()]
    out = compose_mod.compose(store, ids, "单元测试卷", tmp_path / "paper.docx")

    with zipfile.ZipFile(out) as zf:
        names = set(zf.namelist())
        doc = etree.fromstring(zf.read("word/document.xml"))
        rels = etree.fromstring(zf.read("word/_rels/document.xml.rels"))
        targets = {r.get("Id"): r.get("Target") for r in rels}
        # 每个 r:embed/r:id 都能解析到包内真实部件
        used = set()
        for el in doc.iter():
            for attr in parse.REL_ATTRS:
                if el.get(attr):
                    used.add(el.get(attr))
        assert used, "composed doc lost all relationship refs"
        for rid in used:
            assert rid in targets, rid
            assert "word/" + targets[rid] in names, targets[rid]
        # OLE + WMF 都搬进去了
        assert any(n.startswith("word/embeddings/") for n in names)
        assert any(n.endswith(".wmf") for n in names)
        # 题干文本在,且重编号为 1..3
        text = parse.node_text(doc)
        assert "五条线段" in text and "参考答案" in text
        assert "1．五条线段" in text
    store.dispose()


def _para(*texts: str):
    p = etree.Element(f"{{{parse.W}}}p")
    for txt in texts:
        r = etree.SubElement(p, f"{{{parse.W}}}r")
        etree.SubElement(r, f"{{{parse.W}}}t").text = txt
    return p


def test_renumber_survives_split_runs():
    # 真实语料里题号常被拆成 "3" + "．如图" 两个 run
    p = _para("3", "．如图，在△ABC中")
    compose_mod._renumber([p], 1)
    assert parse.node_text(p) == "1．如图，在△ABC中"

    # 题号正好占满所有 run
    p1 = _para("5．")
    compose_mod._renumber([p1], 2)
    assert parse.node_text(p1) == "2．"

    # 单 run 的常规情况不被破坏
    p2 = _para("3．已知")
    compose_mod._renumber([p2], 7)
    assert parse.node_text(p2) == "7．已知"

    # 无字面题号:前插
    p3 = _para("（1）求值")
    compose_mod._renumber([p3], 4)
    assert parse.node_text(p3) == "4．（1）求值"
