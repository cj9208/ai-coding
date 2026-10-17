"""组卷:把选中题目的 verbatim OOXML 片段拼成一份新 .docx(设计 §6.3/D-4)。

为什么是包级手术而不是 python-docx:题目里的公式是 MathType OLE
(``w:object`` = embeddings/*.bin + media/*.wmf + 两个 rId),图是 media/*.png,
python-docx 不碰 ``w:object``,重建必丢公式。这里直接操纵 zip 包:

1. 以最小模板(styles/theme 省略,Word 可打开)为骨架;
2. 依次注入每题 stem_xml,重编号 1..N(只改首个 ``w:t`` 的前导编号);
3. 追加「参考答案」区;
4. 按 sha 去重把引用的 media/embeddings 拷入新包,分配新 rId 并改写片段内
   所有 ``r:embed``/``r:id``;同步 ``document.xml.rels`` 与 ``[Content_Types]``。
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

from lxml import etree

from . import config, parse
from .store import Question, Store

W = parse.W
R = parse.R

_DEFAULT_CT = {
    "rels": "application/vnd.openxmlformats-package.relationships+xml",
    "xml": "application/xml",
    "wmf": "image/x-wmf",
    "emf": "image/x-emf",
    "png": "image/png",
    "jpeg": "image/jpeg",
    "jpg": "image/jpeg",
    "bin": "application/vnd.openxmlformats-officedocument.oleObject",
}
_OVERRIDE_CT = {
    "/word/document.xml": (
        "application/vnd.openxmlformats-officedocument"
        ".wordprocessingml.document.main+xml"
    ),
    "/word/styles.xml": (
        "application/vnd.openxmlformats-officedocument" ".wordprocessingml.styles+xml"
    ),
}


def _content_types_xml() -> str:
    defaults = "".join(
        f'<Default Extension="{e}" ContentType="{c}"/>\n'
        for e, c in _DEFAULT_CT.items()
    )
    overrides = "".join(
        f'<Override PartName="{p}" ContentType="{c}"/>\n'
        for p, c in _OVERRIDE_CT.items()
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
        'content-types">\n' + defaults + overrides + "</Types>"
    )


_NS_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_NS_DOC = "http://schemas.openxmlformats.org/officeDocument/2006"
_NS_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

_ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Relationships xmlns="{_NS_REL}">\n'
    f'<Relationship Id="rId1" Type="{_NS_DOC}/relationships/officeDocument"'
    ' Target="word/document.xml"/>\n'
    "</Relationships>"
)

_STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<w:styles xmlns:w="{_NS_W}">\n'
    "<w:docDefaults><w:rPrDefault><w:rPr>"
    '<w:rFonts w:ascii="Calibri" w:eastAsia="宋体" w:hAnsi="Calibri"/>'
    '<w:sz w:val="21"/></w:rPr></w:rPrDefault></w:docDefaults>\n'
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal">'
    '<w:name w:val="Normal"/></w:style>\n'
    "</w:styles>"
)

_NSMAP = {"w": W, "r": R}


def _make_para(text: str, bold: bool = False):
    p = etree.Element(f"{{{W}}}p", nsmap=_NSMAP)
    if bold:
        ppr = etree.SubElement(p, f"{{{W}}}pPr")
        rpr = etree.SubElement(ppr, f"{{{W}}}rPr")
        etree.SubElement(rpr, f"{{{W}}}b")
    r = etree.SubElement(p, f"{{{W}}}r")
    t = etree.SubElement(r, f"{{{W}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return p


def _renumber(nodes: list, n: int) -> None:
    """把片段首段的前导题号改成 n;无字面题号则前插。

    题号常被拆成多个 run(如 "3" + "．如图"),所以要按整段拼接后的文本定位,
    再把被吃掉的 run 清空。
    """
    if not nodes:
        return
    ts = list(nodes[0].iter(parse.TAG_T))
    joined = "".join(t.text or "" for t in ts)
    if not joined.strip():
        return
    new = f"{n}．"
    m = parse.QNO_RE.match(joined)
    cut = m.end() if m else 0
    consumed = 0
    for t in ts:
        text = t.text or ""
        if consumed >= cut:
            t.text = new + text
            return
        if consumed + len(text) <= cut:
            consumed += len(text)
            t.text = ""
        else:
            t.text = new + text[cut - consumed :]
            return
    ts[-1].text = new


class _Package:
    """新 docx 包的累积器:body 节点 + 部件 + 关系。"""

    def __init__(self) -> None:
        self.body: list = []
        self.parts: dict[str, bytes] = {}  # 包内路径 -> 字节
        self.rels: list[tuple[str, str, str]] = []  # (rId, type, target)
        self._next_rel = 100
        self._seen_sha: dict[str, str] = {}  # sha -> 新包内路径

    def new_rel_id(self) -> str:
        self._next_rel += 1
        return f"rId{self._next_rel}"

    def add_part(self, sha: str, ext: str, data: bytes, kind: str) -> str:
        """按 sha 去重落部件,返回新包内路径。"""
        if sha in self._seen_sha:
            return self._seen_sha[sha]
        idx = len(self._seen_sha) + 1
        sub = "embeddings" if kind == "ole" else "media"
        path = f"word/{sub}/part{idx}.{ext}"
        self.parts[path] = data
        self._seen_sha[sha] = path
        return path

    def add_rel(self, rid: str, target: str, kind: str) -> None:
        rtype = (
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/oleObject"
            if kind == "ole"
            else "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
        )
        self.rels.append((rid, rtype, target))


def _rewrite_rel_attrs(node, mapping: dict[str, str]) -> None:
    for el in node.iter():
        for attr in parse.REL_ATTRS:
            v = el.get(attr)
            if v in mapping:
                el.set(attr, mapping[v])


def _fragment_nodes(xml: str) -> list:
    wrap = etree.fromstring(f"<wrap xmlns:w='{W}' xmlns:r='{R}'>{xml}</wrap>")
    return list(wrap)


def compose(store: Store, question_ids: list[int], title: str, out_path: Path) -> Path:
    pkg = _Package()
    mappings: dict[int, dict[str, str]] = {}
    with store.client.session() as db:
        for n, qid in enumerate(question_ids, start=1):
            q = db.get(Question, qid)
            if q is None:
                continue
            mappings[qid] = _register_media(pkg, q)
            nodes = _fragment_nodes(q.stem_xml)
            _renumber(nodes, n)
            for node in nodes:
                _rewrite_rel_attrs(node, mappings[qid])
            pkg.body.extend(nodes)
        pkg.body.append(_make_para("参考答案", bold=True))
        for n, qid in enumerate(question_ids, start=1):
            q = db.get(Question, qid)
            if q is None or not q.answer_xml:
                continue
            nodes = _fragment_nodes(q.answer_xml)
            _renumber(nodes, n)
            for node in nodes:
                _rewrite_rel_attrs(node, mappings.get(qid, {}))
            pkg.body.extend(nodes)

    _write_package(pkg, title, out_path)
    return out_path


def _register_media(pkg: _Package, q: Question) -> dict[str, str]:
    """把题引用的部件(题干∪答案)去重拷入新包,返回 原rId→新rId。"""
    mapping: dict[str, str] = {}
    for entry in json.loads(q.manifest_json or "[]"):
        data = _media_bytes(entry["sha"], entry["ext"])
        if data is None:
            continue
        path = pkg.add_part(entry["sha"], entry["ext"], data, entry["role"])
        rid = pkg.new_rel_id()
        pkg.add_rel(rid, path.split("word/", 1)[1], entry["role"])
        mapping[entry["rel_id"]] = rid
    return mapping


def _media_bytes(sha: str, ext: str) -> bytes | None:
    p = config.media_path(sha, ext)
    return p.read_bytes() if p.exists() else None


def _write_package(pkg: _Package, title: str, out_path: Path) -> None:
    doc = etree.Element(f"{{{W}}}document", nsmap=_NSMAP)
    body = etree.SubElement(doc, f"{{{W}}}body")
    if title:
        body.append(_make_para(title, bold=True))
    for node in pkg.body:
        body.append(node)
    etree.SubElement(body, f"{{{W}}}sectPr")

    rels = [
        (
            "rId1",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles",
            "styles.xml",
        ),
        *pkg.rels,
    ]
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + "".join(
            f'<Relationship Id="{i}" Type="{t}" Target="{tg}"/>' for i, t, tg in rels
        )
        + "</Relationships>"
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", _content_types_xml())
        zf.writestr("_rels/.rels", _ROOT_RELS)
        zf.writestr("word/document.xml", etree.tostring(doc, encoding="unicode"))
        zf.writestr("word/_rels/document.xml.rels", rels_xml)
        zf.writestr("word/styles.xml", _STYLES)
        for path, data in pkg.parts.items():
            zf.writestr(path, data)
