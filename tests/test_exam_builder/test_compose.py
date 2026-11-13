"""compose 包级手术的往返/完整性测试(设计 §6.3 验收)。

不依赖用户语料:用 fixture 现场造一个最小 docx(含 OLE 公式 + WMF 预览),
ingest 后 compose,再断言新包里 关系可解析、部件齐全、题干文本在、题号重排。
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from lxml import etree

from exam_builder import cli as cli_mod
from exam_builder import compose as compose_mod
from exam_builder import config, ingest, parse, web
from exam_builder.store import Paper, Question, Store

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


@pytest.fixture
def compose_case(tmp_path, monkeypatch):
    database = tmp_path / "data" / "exam.db"
    monkeypatch.setattr(config, "db_path", lambda: database)
    monkeypatch.setattr(
        config, "media_path", lambda sha, ext: tmp_path / "media" / f"{sha}.{ext}"
    )
    store = Store(database)
    try:
        report = ingest.ingest_file(store, _make_docx(tmp_path / "src.docx"))
        assert report.status == "ok"
        assert report.n_questions == 3
        with store.client.session() as db:
            questions = db.query(Question).order_by(Question.id).all()
            ids = [q.id for q in questions]
            entries = [
                {"qid": q.id, **entry}
                for q in questions
                for entry in json.loads(q.manifest_json)
            ]
        assert {entry["ext"] for entry in entries} == {"bin", "wmf"}
        yield store, ids, entries
    finally:
        store.dispose()


def _prepare_output(store, ids, out: Path, existing: bool):
    if existing:
        compose_mod.compose(store, ids, "原有试卷", out)
        return out.read_bytes()
    assert not out.exists()
    return None


def _assert_output_unchanged(out: Path, before):
    if before is None:
        assert not out.exists()
    else:
        assert out.read_bytes() == before
    assert list(out.parent.glob(f".{out.name}.*.tmp")) == []


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("ext", ["bin", "wmf"])
@pytest.mark.parametrize(
    ("fault", "message"),
    [
        ("missing", "无法读取媒体"),
        ("changed", "媒体校验失败"),
        ("empty", "媒体校验失败"),
        ("unreadable", "无法读取媒体"),
    ],
)
def test_media_failure_preserves_output(
    compose_case, tmp_path, monkeypatch, existing, ext, fault, message
):
    store, ids, entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, existing)
    entry = next(item for item in entries if item["ext"] == ext)
    media = config.media_path(entry["sha"], entry["ext"])
    if fault == "missing":
        media.rename(media.with_suffix(media.suffix + ".hidden"))
    elif fault == "changed":
        media.write_bytes(b"damaged media")
    elif fault == "empty":
        media.write_bytes(b"")
    else:
        read_bytes = Path.read_bytes

        def deny_selected(path):
            if path == media:
                raise PermissionError("fixture read denied")
            return read_bytes(path)

        monkeypatch.setattr(Path, "read_bytes", deny_selected)

    with pytest.raises(compose_mod.ComposeError, match=message) as caught:
        compose_mod.compose(store, ids, "不应生成", out)
    error = str(caught.value)
    assert f"题目 {entry['qid']}" in error
    assert entry["rel_id"] in error
    assert entry["sha"] in error
    assert ext in error
    _assert_output_unchanged(out, before)


@pytest.mark.parametrize(
    "fault", ["json", "object", "field", "sha", "ext", "role", "duplicate"]
)
def test_invalid_manifest_preserves_output(compose_case, tmp_path, fault):
    store, ids, entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, True)
    entry = entries[0]
    with store.client.session() as db:
        question = db.get(Question, entry["qid"])
        assert question is not None
        rows = json.loads(question.manifest_json)
        if fault == "json":
            question.manifest_json = "["
        elif fault == "object":
            question.manifest_json = "{}"
        else:
            if fault == "field":
                rows[0].pop("sha")
            elif fault == "sha":
                rows[0]["sha"] = "../outside"
            elif fault == "ext":
                rows[0]["ext"] = "../../outside"
            elif fault == "role":
                rows[0]["role"] = "unknown"
            else:
                rows.append(dict(rows[0]))
            question.manifest_json = json.dumps(rows)
        db.commit()
    with pytest.raises(compose_mod.ComposeError):
        compose_mod.compose(store, ids, "不应生成", out)
    _assert_output_unchanged(out, before)


@pytest.mark.parametrize("section", ["stem_xml", "answer_xml"])
@pytest.mark.parametrize("rid", ["rId1", "rId101", "rIdMissing"])
def test_unmapped_fragment_relationship_is_rejected(
    compose_case, tmp_path, section, rid
):
    store, ids, entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, True)
    with store.client.session() as db:
        question = db.get(Question, entries[0]["qid"])
        assert question is not None
        nodes = compose_mod._fragment_nodes(question.stem_xml)
        for node in nodes:
            for el in node.iter():
                for attr in parse.REL_ATTRS:
                    if el.get(attr) is not None:
                        el.set(attr, rid)
        fragment = parse.serialize(nodes)
        question.stem_xml = parse.serialize([_para("1．纯文字题干")])
        question.answer_xml = ""
        setattr(question, section, fragment)
        question.manifest_json = "[]"
        db.commit()
    with pytest.raises(compose_mod.ComposeError, match="未登记到 manifest"):
        compose_mod.compose(store, ids, "不应生成", out)
    _assert_output_unchanged(out, before)


@pytest.mark.parametrize("section", ["stem_xml", "answer_xml"])
def test_invalid_fragment_is_rejected(compose_case, tmp_path, section):
    store, ids, entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, True)
    with store.client.session() as db:
        question = db.get(Question, entries[0]["qid"])
        assert question is not None
        setattr(question, section, "<w:p>")
        db.commit()
    with pytest.raises(compose_mod.ComposeError, match="OOXML 片段无效"):
        compose_mod.compose(store, ids, "不应生成", out)
    _assert_output_unchanged(out, before)


def _read_parts(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _write_parts(path: Path, parts: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in parts.items():
            archive.writestr(name, data)


def _break_package(path: Path, fault: str) -> None:
    if fault == "bad_zip":
        path.write_bytes(b"not a zip")
        return
    parts = _read_parts(path)
    rel_name = "word/_rels/document.xml.rels"
    rels = etree.fromstring(parts[rel_name])
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    if fault == "missing_media":
        parts.pop(next(name for name in parts if name.endswith(".wmf")))
    elif fault == "missing_styles":
        parts.pop("word/styles.xml")
    elif fault == "missing_main":
        parts.pop("word/document.xml")
    elif fault == "duplicate_id":
        etree.SubElement(rels, f"{{{rel_ns}}}Relationship", dict(rels[0].attrib))
    elif fault == "dangling_id":
        doc = etree.fromstring(parts["word/document.xml"])
        doc.find(f"{{{parse.W}}}body").set(f"{{{parse.R}}}id", "rIdAbsent")
        parts["word/document.xml"] = etree.tostring(doc)
    elif fault == "bad_xml":
        parts["word/document.xml"] = b"<broken>"
    elif fault == "bad_rels":
        # lxml re-attaches the default namespace when only the tag is renamed,
        # so rebuild the root without a namespace to actually break the file.
        bare = etree.Element("Relationships")
        for child in list(rels):
            bare.append(child)
        rels = bare
    elif fault == "orphan_rels":
        parts["word/_rels/absent.xml.rels"] = parts[rel_name]
    elif fault == "unused_target":
        etree.SubElement(
            rels,
            f"{{{rel_ns}}}Relationship",
            Id="rIdUnused",
            Type=_IMG,
            Target="media/absent.png",
        )
    elif fault == "escape_root":
        rels[0].set("Target", "../../outside.xml")
    elif fault == "internal_url":
        rels[0].set("Target", "https://example.invalid/styles.xml")
    elif fault == "bad_mode":
        rels[0].set("TargetMode", "external")
    elif fault == "missing_content_type":
        types = etree.fromstring(parts["[Content_Types].xml"])
        for child in list(types):
            if child.get("Extension") == "wmf":
                types.remove(child)
        parts["[Content_Types].xml"] = etree.tostring(types)
    else:
        raise AssertionError(f"unknown test fault: {fault}")
    parts[rel_name] = etree.tostring(rels)
    _write_parts(path, parts)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        "bad_zip",
        "missing_media",
        "missing_styles",
        "missing_main",
        "duplicate_id",
        "dangling_id",
        "bad_xml",
        "bad_rels",
        "orphan_rels",
        "unused_target",
        "escape_root",
        "internal_url",
        "bad_mode",
        "missing_content_type",
    ],
)
def test_package_rejection_preserves_output(
    compose_case, tmp_path, monkeypatch, existing, fault
):
    store, ids, unused_entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, existing)
    validate = getattr(compose_mod, "_validate_package", None)

    def damage_and_validate(candidate):
        assert candidate != out
        assert candidate.parent == out.parent
        _break_package(candidate, fault)
        assert validate is not None
        validate(candidate)

    monkeypatch.setattr(
        compose_mod, "_validate_package", damage_and_validate, raising=False
    )
    with pytest.raises(compose_mod.ComposeError):
        compose_mod.compose(store, ids, "不应生成", out)
    _assert_output_unchanged(out, before)


@pytest.mark.parametrize("existing", [False, True])
def test_write_failure_preserves_output(compose_case, tmp_path, monkeypatch, existing):
    store, ids, unused_entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, existing)
    original = zipfile.ZipFile.writestr
    calls = 0

    def fail_after_two_parts(archive, name, data, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("fixture disk full")
        return original(archive, name, data, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "writestr", fail_after_two_parts)
    with pytest.raises(compose_mod.ComposeError, match="写出试卷失败"):
        compose_mod.compose(store, ids, "不应生成", out)
    _assert_output_unchanged(out, before)


@pytest.mark.parametrize("existing", [False, True])
def test_replace_failure_preserves_output(
    compose_case, tmp_path, monkeypatch, existing
):
    store, ids, unused_entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, existing)

    def deny_replace(source, destination):
        assert Path(source).parent == out.parent
        assert Path(destination) == out
        raise PermissionError("fixture destination locked")

    monkeypatch.setattr(compose_mod.os, "replace", deny_replace)
    with pytest.raises(compose_mod.ComposeError, match="写出试卷失败"):
        compose_mod.compose(store, ids, "不应生成", out)
    _assert_output_unchanged(out, before)


@pytest.mark.parametrize(
    "target",
    [
        "media/image%20space.wmf",
        "./media/../media/image%20space.wmf",
        "../word/media/image%20space.wmf",
        "/word/media/image%20space.wmf#preview",
    ],
)
def test_valid_targets_external_and_scoped_ids(compose_case, tmp_path, target):
    store, ids, unused_entries = compose_case
    out = compose_mod.compose(store, ids, "关系路径回归", tmp_path / "paper.docx")
    parts = _read_parts(out)
    old_media = next(name for name in parts if name.endswith(".wmf"))
    media = "word/media/image space.wmf"
    parts[media] = parts.pop(old_media)
    rel_ns = "http://schemas.openxmlformats.org/package/2006/relationships"
    rel_name = "word/_rels/document.xml.rels"
    rels = etree.fromstring(parts[rel_name])
    for rel in rels:
        if rel.get("Type") == _IMG:
            rel.set("Target", target)
    etree.SubElement(
        rels,
        f"{{{rel_ns}}}Relationship",
        Id="rIdExternal",
        Type=_OFFICE + "/hyperlink",
        Target="https://example.invalid/not-in-package?q=1&lang=zh#part",
        TargetMode="External",
    )
    etree.SubElement(
        rels,
        f"{{{rel_ns}}}Relationship",
        Id="rIdHeader",
        Type=_OFFICE + "/header",
        Target="headers/header.xml",
        TargetMode="Internal",
    )
    parts[rel_name] = etree.tostring(rels)
    doc = etree.fromstring(parts["word/document.xml"])
    body = doc.find(f"{{{parse.W}}}body")
    paragraph = etree.SubElement(body, f"{{{parse.W}}}p")
    hyperlink = etree.SubElement(paragraph, f"{{{parse.W}}}hyperlink")
    hyperlink.set(f"{{{parse.R}}}id", "rIdExternal")
    link = etree.SubElement(paragraph, f"{{{parse.W}}}drawing")
    link.set(f"{{{parse.R}}}link", "rIdExternal")
    reference = etree.SubElement(body, f"{{{parse.W}}}headerReference")
    reference.set(f"{{{parse.R}}}id", "rIdHeader")
    parts["word/document.xml"] = etree.tostring(doc)
    header = etree.Element(f"{{{parse.W}}}hdr")
    drawing = etree.SubElement(header, f"{{{parse.W}}}drawing")
    drawing.set(f"{{{parse.R}}}embed", "rId1")
    parts["word/headers/header.xml"] = etree.tostring(header)
    header_rels = etree.Element(f"{{{rel_ns}}}Relationships")
    etree.SubElement(
        header_rels,
        f"{{{rel_ns}}}Relationship",
        Id="rId1",
        Type=_IMG,
        Target="../media/image%20space.wmf",
    )
    parts["word/headers/_rels/header.xml.rels"] = etree.tostring(header_rels)
    _write_parts(out, parts)
    compose_mod._validate_package(out)


@pytest.mark.parametrize(
    "target",
    [
        "../../outside.xml",
        "%2e%2e/%2e%2e/outside.xml",
        "media\\image.wmf",
        "https://example.invalid/image.wmf",
        "//example.invalid/image.wmf",
        "media/image.wmf?download=1",
        "media/%ZZ.wmf",
    ],
)
def test_invalid_internal_target_is_rejected(target):
    with pytest.raises(compose_mod.ComposeError):
        compose_mod._resolve_target("word/document.xml", target)


def test_dedup_preserves_ole_preview_and_source_fragments(compose_case, tmp_path):
    store, ids, entries = compose_case
    with store.client.session() as db:
        question = db.get(Question, entries[0]["qid"])
        assert question is not None
        question.answer_xml = question.stem_xml
        db.commit()
        before = [
            (q.id, q.stem_xml, q.answer_xml, q.manifest_json)
            for q in db.query(Question).order_by(Question.id).all()
        ]
    out = compose_mod.compose(store, ids + ids, "去重回归", tmp_path / "paper.docx")
    compose_mod._validate_package(out)
    with zipfile.ZipFile(out) as archive:
        assert archive.testzip() is None
        binaries = [
            name
            for name in archive.namelist()
            if name.startswith(("word/media/", "word/embeddings/"))
        ]
        assert len(binaries) == 2
        assert {archive.read(name) for name in binaries} == {b"OLEDUMMY", b"WMFDUMMY"}
        rels = etree.fromstring(archive.read("word/_rels/document.xml.rels"))
        assert sum(rel.get("Type") == _OLE for rel in rels) == 2
        assert sum(rel.get("Type") == _IMG for rel in rels) == 2
        doc = etree.fromstring(archive.read("word/document.xml"))
        objects = list(doc.iter("{urn:schemas-microsoft-com:office:office}OLEObject"))
        assert len(objects) == 4
        targets = {rel.get("Id"): rel.get("Target") for rel in rels}
        for obj in objects:
            rid = obj.get(f"{{{parse.R}}}id")
            assert archive.read("word/" + targets[rid]) == b"OLEDUMMY"
    with store.client.session() as db:
        after = [
            (q.id, q.stem_xml, q.answer_xml, q.manifest_json)
            for q in db.query(Question).order_by(Question.id).all()
        ]
    assert after == before


def test_publish_occurs_only_after_closed_package_validation(
    compose_case, tmp_path, monkeypatch
):
    store, ids, unused_entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, True)
    validate = compose_mod._validate_package
    replace = compose_mod.os.replace
    events = []

    def checked(candidate):
        assert candidate != out
        assert candidate.parent == out.parent
        assert out.read_bytes() == before
        validate(candidate)
        events.append("validated")

    def publish(source, destination):
        assert events == ["validated"]
        with zipfile.ZipFile(source) as archive:
            assert archive.testzip() is None
        assert out.read_bytes() == before
        replace(source, destination)
        events.append("replaced")

    monkeypatch.setattr(compose_mod, "_validate_package", checked)
    monkeypatch.setattr(compose_mod.os, "replace", publish)
    result = compose_mod.compose(store, ids, "新试卷标题", out)
    assert result == out
    assert events == ["validated", "replaced"]
    with zipfile.ZipFile(out) as archive:
        doc = etree.fromstring(archive.read("word/document.xml"))
        assert "新试卷标题" in parse.node_text(doc)
    assert list(out.parent.glob(f".{out.name}.*.tmp")) == []


def _boundary_fault(entries, monkeypatch, fault):
    entry = entries[0]
    if fault == "missing":
        config.media_path(entry["sha"], entry["ext"]).unlink()
    elif fault == "corrupt":
        config.media_path(entry["sha"], entry["ext"]).write_bytes(b"damaged")
    elif fault == "package":
        validate = compose_mod._validate_package

        def reject_broken(candidate):
            _break_package(candidate, "missing_styles")
            validate(candidate)

        monkeypatch.setattr(compose_mod, "_validate_package", reject_broken)
    else:
        raise AssertionError(f"unknown boundary fault: {fault}")


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fault", ["missing", "corrupt", "package"])
def test_cli_compose_failure_is_visible(
    compose_case, tmp_path, monkeypatch, existing, fault
):
    store, ids, entries = compose_case
    out = tmp_path / "paper.docx"
    before = _prepare_output(store, ids, out, existing)
    _boundary_fault(entries, monkeypatch, fault)
    monkeypatch.setattr(cli_mod, "Store", lambda: store)
    disposed = []
    dispose = store.dispose

    def record_dispose():
        disposed.append(True)
        dispose()

    monkeypatch.setattr(store, "dispose", record_dispose)
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "compose",
            "--ids",
            ",".join(map(str, ids)),
            "--title",
            "失败卷",
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 1
    assert "组卷失败" in result.output
    assert "wrote " not in result.output
    assert "Traceback" not in result.output
    if fault in {"missing", "corrupt"}:
        assert entries[0]["sha"] in result.output
    assert disposed == [True]
    _assert_output_unchanged(out, before)


def test_cli_compose_success_remains_visible(compose_case, tmp_path, monkeypatch):
    store, ids, unused_entries = compose_case
    monkeypatch.setattr(cli_mod, "Store", lambda: store)
    out = tmp_path / "paper.docx"
    result = CliRunner().invoke(
        cli_mod.cli, ["compose", "--ids", ",".join(map(str, ids)), "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    assert f"wrote {out}" in result.output
    compose_mod._validate_package(out)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("fault", ["missing", "corrupt", "package"])
def test_web_compose_failure_has_no_download_or_paper(
    compose_case, monkeypatch, existing, fault
):
    store, ids, entries = compose_case
    stem = "-".join(map(str, ids))[:60]
    out = config.db_path().parent / "papers" / f"paper-{stem}.docx"
    before = _prepare_output(store, ids, out, existing)
    _boundary_fault(entries, monkeypatch, fault)
    with TestClient(web.create_app(store)) as client:
        response = client.post(
            "/compose",
            data={"ids": ",".join(map(str, ids)), "title": "不应生成"},
            follow_redirects=False,
        )
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("text/plain")
    assert "组卷失败" in response.text
    assert "未生成新文件" in response.text
    assert "content-disposition" not in response.headers
    assert "location" not in response.headers
    assert not response.content.startswith(b"PK")
    if fault in {"missing", "corrupt"}:
        assert entries[0]["sha"] in response.text
    with store.client.session() as db:
        assert db.query(Paper).count() == 0
    _assert_output_unchanged(out, before)


def test_web_compose_success_downloads_and_records_paper(compose_case):
    store, ids, unused_entries = compose_case
    with TestClient(web.create_app(store)) as client:
        response = client.post(
            "/compose",
            data={"ids": ",".join(map(str, ids)), "title": "正常试卷"},
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert archive.testzip() is None
        binaries = [
            archive.read(name)
            for name in archive.namelist()
            if name.startswith(("word/media/", "word/embeddings/"))
        ]
        assert set(binaries) == {b"OLEDUMMY", b"WMFDUMMY"}
    with store.client.session() as db:
        papers = db.query(Paper).all()
        assert len(papers) == 1
        assert papers[0].title == "正常试卷"
        assert Path(papers[0].out_path).is_file()
