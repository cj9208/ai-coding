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
import os
import posixpath
import re
import tempfile
import zipfile
import zlib
from pathlib import Path
from urllib.parse import unquote, urlsplit

from lxml import etree

from storage import sha256_hex

from . import config, parse
from .store import Question, Store


class ComposeError(Exception):
    """素材或候选包不完整，或试卷无法安全发布。"""


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
            value = el.get(attr)
            if value is None:
                continue
            if value not in mapping:
                raise ComposeError(f"片段关系 {value!r} 未登记到 manifest")
            el.set(attr, mapping[value])


def _fragment_nodes(xml: str) -> list:
    try:
        wrap = etree.fromstring(
            f"<wrap xmlns:w='{W}' xmlns:r='{R}'>{xml}</wrap>",
            parser=etree.XMLParser(resolve_entities=False, no_network=True),
        )
    except etree.XMLSyntaxError as exc:
        raise ComposeError(f"题目 OOXML 片段无效：{exc}") from exc
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
    """严格搬运题干与答案的媒体并返回原 rId 到新 rId 的映射。"""
    try:
        entries = json.loads(q.manifest_json or "[]")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ComposeError(f"题目 {q.id} 的 manifest 不是有效 JSON") from exc
    if not isinstance(entries, list):
        raise ComposeError(f"题目 {q.id} 的 manifest 必须是列表")
    mapping: dict[str, str] = {}
    for entry in entries:
        required = ("sha", "ext", "rel_id", "role")
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(key), str) or not entry[key] for key in required
        ):
            raise ComposeError(f"题目 {q.id} 的 manifest 条目字段不完整")
        old_rid = entry["rel_id"]
        if old_rid in mapping:
            raise ComposeError(f"题目 {q.id} 的 manifest 重复关系：{old_rid}")
        if entry["role"] not in {"figure", "formula_preview", "ole"}:
            raise ComposeError(f"题目 {q.id} 的 manifest 角色无效：{entry['role']}")
        try:
            data = _media_bytes(entry["sha"], entry["ext"])
        except ComposeError as exc:
            raise ComposeError(f"题目 {q.id}，关系 {old_rid}：{exc}") from exc
        path = pkg.add_part(entry["sha"], entry["ext"], data, entry["role"])
        rid = pkg.new_rel_id()
        pkg.add_rel(rid, path.split("word/", 1)[1], entry["role"])
        mapping[old_rid] = rid
    return mapping


def _media_bytes(sha: str, ext: str) -> bytes:
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ComposeError(f"媒体摘要格式无效：{sha!r}")
    if not re.fullmatch(r"[a-zA-Z0-9]+", ext):
        raise ComposeError(f"媒体扩展名无效：{ext!r}")
    path = config.media_path(sha, ext)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ComposeError(f"无法读取媒体 {path}：{exc}") from exc
    if not data or sha256_hex(data) != sha:
        raise ComposeError(f"媒体校验失败：{path}；预期 SHA-256={sha}")
    return data


def _resolve_target(source: str, target: str) -> str:
    if not target or re.search(r"%(?![0-9a-fA-F]{2})", target):
        raise ComposeError(f"内部关系目标无效：{target!r}")
    try:
        uri = urlsplit(target)
        path = unquote(uri.path, errors="strict")
    except (ValueError, UnicodeError) as exc:
        raise ComposeError(f"内部关系目标无效：{target!r}") from exc
    if uri.scheme or uri.netloc or uri.query:
        raise ComposeError(f"内部关系不能使用外部 URI：{target!r}")
    if "\\" in path or "\x00" in path or path.endswith("/"):
        raise ComposeError(f"内部关系路径无效：{target!r}")
    if not path:
        if source and uri.fragment:
            return source
        raise ComposeError(f"内部关系没有部件路径：{target!r}")
    base = posixpath.dirname(source)
    segments = [] if path.startswith("/") or not base else base.split("/")
    for segment in path.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if not segments:
                raise ComposeError(f"内部关系逃出包根：{target!r}")
            segments.pop()
        else:
            segments.append(segment)
    if not segments:
        raise ComposeError(f"内部关系指向包根而非部件：{target!r}")
    return "/".join(segments)


def _relationship_source(name: str) -> str:
    if name == "_rels/.rels":
        return ""
    directory, filename = posixpath.split(name)
    if (
        posixpath.basename(directory) != "_rels"
        or not filename.endswith(".rels")
        or filename == ".rels"
    ):
        raise ComposeError(f"关系文件位置无效：{name}")
    return posixpath.join(posixpath.dirname(directory), filename[:-5])


def _validate_package(path: Path) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            bad_member = archive.testzip()
            if bad_member is not None:
                raise ComposeError(f"ZIP 校验失败：{bad_member}")
            names = [item.filename for item in archive.infolist() if not item.is_dir()]
            members = set(names)
            if len(names) != len(members):
                raise ComposeError("ZIP 包含重复部件名")
            for name in names:
                if (
                    name.startswith("/")
                    or "\\" in name
                    or any(piece in {"", ".", ".."} for piece in name.split("/"))
                ):
                    raise ComposeError(f"ZIP 部件路径无效：{name}")
            required = {
                "[Content_Types].xml",
                "_rels/.rels",
                "word/document.xml",
                "word/_rels/document.xml.rels",
            }
            missing = required - members
            if missing:
                raise ComposeError(f"ZIP 缺少必需部件：{sorted(missing)}")
            trees = {
                name: etree.fromstring(
                    archive.read(name),
                    parser=etree.XMLParser(resolve_entities=False, no_network=True),
                )
                for name in sorted(members)
                if name.endswith((".xml", ".rels"))
            }
            ct_ns = "http://schemas.openxmlformats.org/package/2006/content-types"
            types = trees["[Content_Types].xml"]
            if types.tag != f"{{{ct_ns}}}Types":
                raise ComposeError("Content_Types 根元素无效")
            defaults = {
                item.get("Extension"): item.get("ContentType")
                for item in types.findall(f"{{{ct_ns}}}Default")
            }
            overrides = {
                item.get("PartName"): item.get("ContentType")
                for item in types.findall(f"{{{ct_ns}}}Override")
            }
            for name in sorted(members - {"[Content_Types].xml"}):
                ext = name.rsplit(".", 1)[-1]
                if not overrides.get("/" + name) and not defaults.get(ext):
                    raise ComposeError(f"部件没有 ContentType：{name}")

            ids_by_source: dict[str, set[str]] = {}
            main_document_found = False
            for name in sorted(members):
                if not name.endswith(".rels"):
                    continue
                source = _relationship_source(name)
                if source and source not in members:
                    raise ComposeError(f"关系源部件缺失：{name} → {source}")
                root = trees[name]
                if root.tag != f"{{{_NS_REL}}}Relationships":
                    raise ComposeError(f"关系文件根元素无效：{name}")
                ids: set[str] = set()
                for rel in root:
                    rid = rel.get("Id")
                    target = rel.get("Target")
                    rtype = rel.get("Type")
                    if (
                        rel.tag != f"{{{_NS_REL}}}Relationship"
                        or not rid
                        or not target
                        or not rtype
                    ):
                        raise ComposeError(f"关系字段不完整：{name}")
                    if rid in ids:
                        raise ComposeError(f"重复关系 ID：{name} / {rid}")
                    ids.add(rid)
                    mode = rel.get("TargetMode", "Internal")
                    if mode == "External":
                        continue
                    if mode != "Internal":
                        raise ComposeError(f"TargetMode 无效：{name} / {rid}")
                    resolved = _resolve_target(source, target)
                    if resolved not in members:
                        raise ComposeError(
                            f"内部关系目标缺失：{name} / {rid} → {resolved}"
                        )
                    if (
                        source == ""
                        and rtype == f"{_NS_DOC}/relationships/officeDocument"
                        and resolved == "word/document.xml"
                    ):
                        main_document_found = True
                ids_by_source[source] = ids
            if not main_document_found:
                raise ComposeError("包根缺少主文档关系")

            for name, root in trees.items():
                if not name.endswith(".xml") or name == "[Content_Types].xml":
                    continue
                known = ids_by_source.get(name, set())
                for element in root.iter():
                    for attribute in parse.REL_ATTRS:
                        rid = element.get(attribute)
                        if rid is not None and rid not in known:
                            raise ComposeError(f"XML 引用未定义关系：{name} / {rid!r}")
    except (
        OSError,
        zipfile.BadZipFile,
        KeyError,
        etree.XMLSyntaxError,
        RuntimeError,
        zlib.error,
        EOFError,
    ) as exc:
        raise ComposeError(f"候选包无法通过完整性校验：{exc}") from exc


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
            f"{_NS_DOC}/relationships/styles",
            "styles.xml",
        ),
        *pkg.rels,
    ]
    rels_root = etree.Element(f"{{{_NS_REL}}}Relationships", nsmap={None: _NS_REL})
    for rid, rtype, target in rels:
        etree.SubElement(
            rels_root,
            f"{{{_NS_REL}}}Relationship",
            Id=rid,
            Type=rtype,
            Target=target,
        )
    temp_path: Path | None = None
    try:
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                dir=out_path.parent,
                prefix=f".{out_path.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temp_path = Path(temporary.name)
            with zipfile.ZipFile(temp_path, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("[Content_Types].xml", _content_types_xml())
                archive.writestr("_rels/.rels", _ROOT_RELS)
                archive.writestr("word/document.xml", etree.tostring(doc))
                archive.writestr(
                    "word/_rels/document.xml.rels", etree.tostring(rels_root)
                )
                archive.writestr("word/styles.xml", _STYLES)
                for path, data in pkg.parts.items():
                    archive.writestr(path, data)
            _validate_package(temp_path)
            os.replace(temp_path, out_path)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ComposeError(f"写出试卷失败（{out_path}）：{exc}") from exc
