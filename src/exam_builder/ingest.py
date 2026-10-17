"""入库管线(设计 §6.1):读 docx → 切题 → 抽媒体到 cache → 写 SQLite。

幂等:同 filename 且 sha256 未变则跳过;sha 变了则整文档重切(先删旧题)。
``.doc`` 老格式不转换、不崩溃,标记 error(D-3/§3.4)。
"""

from __future__ import annotations

import json
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree
from sqlalchemy import func

from storage import sha256_hex

from . import config, parse
from .store import Document, Question, Store, file_sha


@dataclass
class DocReport:
    filename: str
    status: str
    n_questions: int = 0
    n_media: int = 0
    error: str = ""


@dataclass
class IngestReport:
    docs: list[DocReport] = field(default_factory=list)

    @property
    def n_ok(self) -> int:
        return sum(1 for d in self.docs if d.status == "ok")

    @property
    def n_error(self) -> int:
        return sum(1 for d in self.docs if d.status != "ok")

    @property
    def n_questions(self) -> int:
        return sum(d.n_questions for d in self.docs)


def _extract_media(zf: zipfile.ZipFile, part: str) -> tuple[str, str]:
    """把包内部件按 sha 去重落到 cache;返回 (sha, ext)。"""
    data = zf.read(part)
    sha = sha256_hex(data)
    ext = part.rsplit(".", 1)[-1]
    dest = config.media_path(sha, ext)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
    return sha, ext


def ingest_file(store: Store, path: Path, force: bool = False) -> DocReport:
    rep = DocReport(filename=path.name, status="ok")
    if path.suffix.lower() == ".doc":
        rep.status = "error"
        rep.error = "legacy .doc (needs LibreOffice or re-save as .docx)"
        _record(store, path, rep, sha="", questions=[])
        return rep

    sha = file_sha(path)
    with store.client.session() as db:
        existing = db.query(Document).filter(Document.filename == path.name).first()
        if (
            not force
            and existing is not None
            and existing.sha256 == sha
            and existing.status == "ok"
        ):
            rep.status = existing.status
            rep.error = existing.error_msg
            rep.n_questions = (
                db.query(func.count(Question.id))
                .filter(Question.doc_id == existing.id)
                .scalar()
                or 0
            )
            return rep

    with zipfile.ZipFile(path) as zf:
        try:
            root = etree.fromstring(zf.read("word/document.xml"))
        except KeyError:
            rep.status = "error"
            rep.error = "no word/document.xml"
            _record(store, path, rep, sha=sha, questions=[])
            return rep
        rels = parse.read_rels(zf)
        nodes = parse.body_nodes(root)
        result = parse.split(nodes)

        if result.errors:
            rep.status = "error"
            rep.error = "; ".join(result.errors)
            questions: list[dict] = []
        else:
            answers = {b.qno: b for b in result.answers}
            questions = []
            for i, blk in enumerate(result.questions):
                ans = answers.get(blk.qno)
                stem_manifest = parse.manifest_for(blk.nodes, rels)
                # manifest 存 题干∪答案:组卷时答案片段里的图/公式也要搬+重排
                union = {p.rel_id: p for p in stem_manifest}
                if ans:
                    for p in parse.manifest_for(ans.nodes, rels):
                        union.setdefault(p.rel_id, p)
                manifest = []
                for p in sorted(union.values(), key=lambda x: x.rel_id):
                    sha_m, ext = _extract_media(zf, p.part)
                    manifest.append(
                        {
                            "role": p.role,
                            "rel_id": p.rel_id,
                            "part": p.part,
                            "sha": sha_m,
                            "ext": ext,
                        }
                    )
                questions.append(
                    {
                        "qno": blk.qno,
                        "ord": i,
                        "stem_xml": parse.serialize(blk.nodes),
                        "answer_xml": parse.serialize(ans.nodes) if ans else "",
                        "manifest_json": json.dumps(manifest, ensure_ascii=False),
                        "stem_text": parse.stem_text(blk.nodes),
                        "n_figure": sum(1 for m in stem_manifest if m.role == "figure"),
                        "n_formula": sum(1 for m in stem_manifest if m.role == "ole"),
                    }
                )
                rep.n_media += len(manifest)

    if result.errors:
        _record(store, path, rep, sha=sha, questions=[])
        return rep
    rep.n_questions = len(questions)
    _record(store, path, rep, sha=sha, questions=questions)
    return rep


def _record(
    store: Store, path: Path, rep: DocReport, sha: str, questions: list[dict]
) -> None:
    tags = parse.parse_filename(path.name)
    with store.client.session() as db:
        doc = store.upsert_document(
            db,
            filename=path.name,
            source_path=str(path),
            sha256=sha,
            status=rep.status,
            error_msg=rep.error,
            **tags,
        )
        for q in questions:
            store.add_question(db, doc.id, **q)
        db.commit()


def ingest_dir(store: Store, directory: Path, force: bool = False) -> IngestReport:
    report = IngestReport()
    for path in sorted(directory.glob("*.doc*")):
        report.docs.append(ingest_file(store, path, force=force))
    return report
