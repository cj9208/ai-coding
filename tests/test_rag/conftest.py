"""Shared fixtures: synthetic OcrDocuments + a built rag store.

Two bundles so snapshot/publish semantics get exercised with more than one
document; Chinese content because the CJK folding path is the load-bearing
retrieval machinery on this machine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ocr_backend.contract import (
    BlockKind,
    ContentFormat,
    OcrBackendInfo,
    OcrBlock,
    OcrDocument,
    OcrPage,
    OcrSource,
)
from rag.ingest import OcrBundleAcquirer
from rag.pipeline import build
from rag.store import RagStore
from storage import sha256_hex


def block(
    id_: int,
    kind: BlockKind,
    content: str,
    order: int | None = None,
    content_format: ContentFormat = ContentFormat.text,
    score: float | None = 0.95,
) -> OcrBlock:
    return OcrBlock(
        id=id_,
        kind=kind,
        raw_label=str(kind),
        content=content,
        content_format=content_format,
        bbox=(
            40.0,
            60.0 + order * 50 if order is not None else 60.0,
            500.0,
            100.0 + order * 50 if order is not None else 100.0,
        ),
        order=order,
        score=score,
    )


def make_document(text_sha: str, blocks_by_page: list[list[OcrBlock]]) -> OcrDocument:
    pages = [
        OcrPage(page_index=i, width=600, height=800, blocks=blocks)
        for i, blocks in enumerate(blocks_by_page)
    ]
    return OcrDocument(
        source=OcrSource(
            kind="pdf",
            path=f"{text_sha}.pdf",
            sha256=sha256_hex(text_sha),
            page_count=len(pages),
        ),
        backend=OcrBackendInfo(
            name="paddleocr_vl",
            library_version="3.0",
            model="PaddleOCR-VL-1.6",
            pipeline_version="1.0",
            options={},
        ),
        created_at="2026-09-21T10:00:00+08:00",
        pages=pages,
    )


LEAVE_DOC_BLOCKS = [
    block(0, BlockKind.title, "1 年假规定", order=0),
    block(1, BlockKind.paragraph, "员工入职满一年可享受五天年假。", order=1),
    block(2, BlockKind.title, "1.1 审批流程", order=2),
    block(3, BlockKind.paragraph, "年假超过三天需要部门经理审批。", order=3),
    block(
        4,
        BlockKind.table,
        "<table><tr><th>天数</th><th>审批人</th></tr>"
        "<tr><td>3</td><td>主管</td></tr>"
        "<tr><td>5</td><td>经理</td></tr></table>",
        order=4,
        content_format=ContentFormat.html,
    ),
    block(5, BlockKind.header, "公司内部制度", order=5),
    block(6, BlockKind.page_number, "1", order=6),
]

PRICING_DOC_BLOCKS = [
    block(0, BlockKind.title, "2 产品定价", order=0),
    block(1, BlockKind.paragraph, "基础版每年收费 1200 元，专业版 4800 元。", order=1),
    block(2, BlockKind.list_item, "- 学生享八折优惠", order=2),
    block(3, BlockKind.list_item, "- 年付赠送两个月", order=3),
]


def write_bundle(inbox: Path, stem: str, doc: OcrDocument) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / f"{stem}.ocr.json"
    path.write_text(doc.model_dump_json(), encoding="utf-8")
    return path


@pytest.fixture
def inbox(tmp_path: Path) -> Path:
    box = tmp_path / "out"
    write_bundle(box, "leave", make_document("leave", [LEAVE_DOC_BLOCKS]))
    write_bundle(box, "pricing", make_document("pricing", [PRICING_DOC_BLOCKS]))
    return box


@pytest.fixture
def acquirer() -> OcrBundleAcquirer:
    return OcrBundleAcquirer()


@pytest.fixture
def leave_doc(acquirer: OcrBundleAcquirer, inbox: Path):
    return acquirer.fetch(inbox / "leave.ocr.json")


@pytest.fixture
def store(tmp_path: Path):
    s = RagStore(tmp_path / "data" / "kb.db")
    yield s
    s.dispose()


@pytest.fixture
def built(tmp_path: Path, inbox: Path):
    """Full offline pipeline over the fixture inbox."""
    data_dir = tmp_path / "data"
    report = build(inbox, data_dir)
    s = RagStore(data_dir / "kb.db")
    yield s, report
    s.dispose()
