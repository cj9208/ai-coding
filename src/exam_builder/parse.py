"""把一份「同步练习」docx 的 document.xml 切成一道道题(设计 §4 契约)。

切题粒度 = 顶层 body 节点(``w:p`` / ``w:tbl``),因为公式是 MathType OLE、
图是 PNG,它们寄生在段落内部,只有整段搬运才不丢内容(D-1)。

三个表示层的处理:
- 文字 ``w:t``            → 进 stem_text 投影(搜索/展示)
- 几何图 ``w:drawing``    → 投影插〔图〕,字节经 manifest 记录
- 公式 ``w:object``(OLE) → 投影插〔公式〕(MVP 不渲染,D-3),字节经 manifest 记录

锚点与题号规则见设计 §3.2 / D-5:锚点缺失不猜,返回 error 让上层标记文档。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

TAG_P = f"{{{W}}}p"
TAG_TBL = f"{{{W}}}tbl"
TAG_T = f"{{{W}}}t"
TAG_OBJECT = f"{{{W}}}object"
TAG_DRAWING = f"{{{W}}}drawing"
TAG_PICT = f"{{{W}}}pict"
TAG_BODY = f"{{{W}}}body"

#: 段落里指向 docx 包内部件的关系属性(图 / OLE / WMF 预览都走这里)
REL_ATTRS = (f"{{{R}}}embed", f"{{{R}}}id", f"{{{R}}}link")

BODY_TAGS = (TAG_P, TAG_TBL)

Q_ANCHORS = ("同步练习题目",)
A_ANCHORS = ("同步练习答案", "参考答案", "答案")

#: 题号行:行首数字 + 全角/半角句点或顿号。小问「（1）」以（开头,不会误开新题。
QNO_RE = re.compile(r"^\s*(\d+)\s*[．.、]")

FORMULA_PLACEHOLDER = "〔公式〕"
FIGURE_PLACEHOLDER = "〔图〕"


@dataclass
class Part:
    """题目片段引用的一个 docx 包内部件。"""

    role: str  # figure_png | formula_preview | ole
    rel_id: str
    part: str  # 包内路径,如 media/image3.wmf / embeddings/oleObject2.bin


@dataclass
class Block:
    qno: int
    nodes: list = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(node_text(n) for n in self.nodes if node_text(n))


@dataclass
class SplitResult:
    questions: list[Block] = field(default_factory=list)
    answers: list[Block] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def parse_filename(name: str) -> dict:
    """从 ``13.2.1 三角形的边（第2课时）.docx`` 解析结构标签(D-6)。"""
    stem = re.sub(r"\.(docx?|DOCX?)$", "", name)
    lesson = ""
    m = re.search(r"（?(第\d+课时)）?", stem)
    if m:
        lesson = m.group(1)
        stem = stem.replace(m.group(0), "").strip()
    m = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*(.+)$", stem)
    if m:
        chapter, sec2, sec3, title = m.groups()
        section = ".".join(x for x in (chapter, sec2, sec3) if x)
        return {
            "chapter": chapter,
            "section": section,
            "lesson": lesson,
            "title": title,
        }
    m = re.match(r"^第([一二三四五六七八九十\d]+)章\s*(.+)$", stem)
    if m:
        return {
            "chapter": m.group(1),
            "section": "",
            "lesson": lesson,
            "title": m.group(2),
        }
    return {"chapter": "", "section": "", "lesson": lesson, "title": stem}


def _has_anchor(el) -> bool:
    return any(
        _is_anchor(node_text(p), Q_ANCHORS) or _is_anchor(node_text(p), A_ANCHORS)
        for p in el.iter(TAG_P)
    )


def body_nodes(root) -> list:
    """文档序的"块"序列:段落(``w:p``,无论嵌套多深)+ 原子表格。

    语料的正文整份包在一张大表里,题目是表单元格内的段落(§3.2 实测),
    所以必须 descend 进含锚点的表;而题目内部的选项/图形小表不含锚点,
    作为原子块整体保留,版式不丢。
    """
    out: list = []

    def rec(el) -> None:
        for c in el:
            if c.tag == TAG_P:
                out.append(c)
            elif c.tag == TAG_TBL:
                if _has_anchor(c):
                    rec(c)
                else:
                    out.append(c)
            else:
                rec(c)

    rec(root)
    return out


def node_text(node) -> str:
    return "".join(t.text or "" for t in node.iter(TAG_T)).strip()


def _is_anchor(text: str, anchors: tuple) -> bool:
    return any(text == a or text.startswith(a) for a in anchors)


def _has_numpr(node) -> bool:
    """Word 自动编号段落:题号由 numbering.xml 生成,正文里没有字面数字。"""
    return node.find(f".//{{{W}}}numPr") is not None


def _split_region(nodes: list) -> list[Block]:
    """在一段区域内按题号切块;非题号行归入当前块(小问/续行)。

    边界三种来源:字面题号(``1．``)、Word 自动编号(``w:numPr``,序号按位置
    推)、以及区域首段无编号(语料里第 1 题偶发丢号,默认为第 1 题)。
    """
    blocks: list[Block] = []
    for node in nodes:
        text = node_text(node)
        m = QNO_RE.match(text)
        if m:
            blocks.append(Block(qno=int(m.group(1)), nodes=[node]))
        elif _has_numpr(node):
            blocks.append(Block(qno=len(blocks) + 1, nodes=[node]))
        elif not blocks:
            blocks.append(Block(qno=1, nodes=[node]))
        else:
            blocks[-1].nodes.append(node)
    return blocks


def split(nodes: list) -> SplitResult:
    """按锚点把 body 节点分成 题目区 / 答案区,区内再按题号切块(D-5)。"""
    res = SplitResult()
    qi = ai = -1
    for i, node in enumerate(nodes):
        text = node_text(node)
        if qi < 0 and _is_anchor(text, Q_ANCHORS):
            qi = i
        elif qi >= 0 and ai < 0 and _is_anchor(text, A_ANCHORS):
            ai = i
            break
    if qi < 0:
        res.errors.append(f"missing question anchor {Q_ANCHORS}")
        return res
    q_region = nodes[qi + 1 : ai if ai >= 0 else len(nodes)]
    a_region = nodes[ai + 1 :] if ai >= 0 else []
    res.questions = _split_region(q_region)
    res.answers = _split_region(a_region)
    if not res.questions:
        res.errors.append("no questions between anchors")
    unmatched = {b.qno for b in res.answers} - {b.qno for b in res.questions}
    if unmatched:
        res.errors.append(f"answers without question: {sorted(unmatched)}")
    return res


def rel_ids(node) -> set:
    ids = set()
    for el in node.iter():
        for attr in REL_ATTRS:
            v = el.get(attr)
            if v:
                ids.add(v)
    return ids


def classify_part(part: str) -> str:
    """包内路径 → 角色。part 形如 ``word/embeddings/…`` / ``word/media/…``。"""
    if "embeddings/" in part:
        return "ole"
    if part.endswith(".wmf"):
        return "formula_preview"
    return "figure"


def manifest_for(nodes: list, rels: dict) -> list[Part]:
    """片段引用的部件清单;rels = {rel_id: 包内路径}。"""
    seen: dict[str, Part] = {}
    for node in nodes:
        for rid in rel_ids(node):
            part = rels.get(rid)
            if part and rid not in seen:
                seen[rid] = Part(role=classify_part(part), rel_id=rid, part=part)
    return sorted(seen.values(), key=lambda p: p.rel_id)


def _collect(el, out: list) -> None:
    tag = el.tag
    if tag == TAG_T:
        out.append(el.text or "")
        return
    if tag == TAG_OBJECT:
        out.append(FORMULA_PLACEHOLDER)
        return
    if tag in (TAG_DRAWING, TAG_PICT):
        out.append(FIGURE_PLACEHOLDER)
        return
    for child in el:
        _collect(child, out)


def stem_text(nodes: list) -> str:
    """搜索/展示用的纯文本投影;公式与图只留占位符(D-3)。"""
    lines = []
    for node in nodes:
        out: list[str] = []
        _collect(node, out)
        line = "".join(out).strip()
        if line:
            lines.append(line)
    return "\n".join(lines)


def serialize(nodes: list) -> str:
    """verbatim OOXML 片段:顶层节点原样序列化(D-1)。"""
    return "".join(etree.tostring(n, encoding="unicode") for n in nodes)


def read_rels(zip_file, name: str = "word/_rels/document.xml.rels") -> dict:
    """{rel_id: 包内路径},只取 image / oleObject 两类目标。"""
    try:
        raw = zip_file.read(name)
    except KeyError:
        return {}
    root = etree.fromstring(raw)
    out = {}
    for rel in root:
        rid = rel.get("Id")
        target = rel.get("Target") or ""
        rtype = rel.get("Type") or ""
        if not rid:
            continue
        if rtype.endswith("/image") or rtype.endswith("/oleObject"):
            # Target 相对 word/ 解析;以 / 开头则是包根绝对路径
            out[rid] = (
                target.lstrip("/") if target.startswith("/") else "word/" + target
            )
    return out
