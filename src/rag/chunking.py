"""Structure-aware parent/child chunking (CH03_02's chunk step).

Rules, in priority order: structure before token windows; tables and
formulas are atomic (never merged, never split); adjacent list items stay
one chunk with their items together; sections become parent chunks that
children can expand into at assembly time. Every chunk keeps ``block_keys``
so a citation can always be traced to page+block, and ids are
content-addressed so a future representation (embeddings) can be joined on
them across rebuilds.
"""

from __future__ import annotations

from ocr_backend.contract import BlockKind, OcrBlock

from .contract import CanonicalDoc, Chunk, ChunkType, block_key
from .structure import FURNITURE_KINDS, reading_order_blocks

CHILD_CHAR_LIMIT = 800

_KIND_TO_TYPE = {
    BlockKind.table: ChunkType.table,
    BlockKind.formula: ChunkType.formula,
    BlockKind.list_item: ChunkType.list,
    BlockKind.paragraph: ChunkType.prose,
    BlockKind.caption: ChunkType.prose,
    BlockKind.footnote: ChunkType.prose,
    BlockKind.title: ChunkType.prose,
}

_ATOMIC = frozenset({ChunkType.table, ChunkType.formula})


def _type_of(kind: BlockKind) -> ChunkType:
    return _KIND_TO_TYPE.get(kind, ChunkType.other)


def _mk_chunk(
    doc_id: str,
    section_path: str,
    chunk_type: ChunkType,
    blocks: list[tuple[int, OcrBlock]],
    trust_level: str,
    fp: str = "",
) -> Chunk:
    keys = [block_key(page, b.id) for page, b in blocks]
    body = "\n\n".join(b.content.strip() for _, b in blocks)
    payload = (
        {"formats": [str(b.content_format) for _, b in blocks]}
        if chunk_type in _ATOMIC
        else None
    )
    return Chunk(
        chunk_id=Chunk.chunk_id_for(doc_id, section_path, keys, fp=fp),
        doc_id=doc_id,
        chunk_type=chunk_type,
        is_parent=chunk_type is ChunkType.section,
        parent_chunk_id=None,
        section_path=section_path,
        text=body,
        structured_payload=payload,
        page_span=(blocks[0][0], blocks[-1][0]),
        block_keys=keys,
        content_hash=Chunk.content_hash_for(body),
        trust_level=trust_level,
    )


def _group_by_section(
    doc: CanonicalDoc,
) -> dict[str, list[tuple[int, OcrBlock]]]:
    groups: dict[str, list[tuple[int, OcrBlock]]] = {}
    for page_index, block in reading_order_blocks(doc.document):
        if block.kind in FURNITURE_KINDS:
            continue
        key = block_key(page_index, block.id)
        path = doc.section_paths.get(key, "")
        groups.setdefault(path, []).append((page_index, block))
    return groups


def _flush_run(
    doc: CanonicalDoc,
    section_path: str,
    run: list[tuple[int, OcrBlock]],
    run_type: ChunkType,
    children: list[Chunk],
    fp: str = "",
) -> None:
    if not run:
        return
    trust = doc.trust.publish_decision.value
    if run_type is ChunkType.list or run_type is ChunkType.prose:
        # split an oversized homogeneous run into <CHILD_CHAR_LIMIT windows
        window: list[tuple[int, OcrBlock]] = []
        size = 0
        for item in run:
            item_len = len(item[1].content)
            if window and size + item_len > CHILD_CHAR_LIMIT:
                children.append(
                    _mk_chunk(doc.doc_id, section_path, run_type, window, trust, fp=fp)
                )
                window, size = [], 0
            window.append(item)
            size += item_len
        if window:
            children.append(
                _mk_chunk(doc.doc_id, section_path, run_type, window, trust, fp=fp)
            )
    else:
        children.append(
            _mk_chunk(doc.doc_id, section_path, run_type, run, trust, fp=fp)
        )


def _mk_parent(
    doc_id: str,
    section_path: str,
    children: list[Chunk],
    trust_level: str,
    fp: str = "",
) -> Chunk:
    body = "\n\n".join(c.text for c in children)
    keys = [k for c in children for k in c.block_keys]
    return Chunk(
        chunk_id=Chunk.chunk_id_for(doc_id, section_path, keys, fp=fp),
        doc_id=doc_id,
        chunk_type=ChunkType.section,
        is_parent=True,
        parent_chunk_id=None,
        section_path=section_path,
        text=body,
        structured_payload=None,
        page_span=(
            min(c.page_span[0] for c in children),
            max(c.page_span[1] for c in children),
        ),
        block_keys=keys,
        content_hash=Chunk.content_hash_for(body),
        trust_level=trust_level,
    )


class StructureAwareChunker:
    """Implements :class:`rag.protocols.ChunkStrategy`."""

    name = "structure_aware"

    def split(self, doc: CanonicalDoc, *, fp: str = "") -> list[Chunk]:
        chunks: list[Chunk] = []
        for section_path, blocks in _group_by_section(doc).items():
            children: list[Chunk] = []
            run: list[tuple[int, OcrBlock]] = []
            run_type: ChunkType | None = None
            for page_index, block in blocks:
                btype = _type_of(block.kind)
                if btype in _ATOMIC:
                    _flush_run(
                        doc,
                        section_path,
                        run,
                        run_type or ChunkType.prose,
                        children,
                        fp=fp,
                    )
                    run, run_type = [], None
                    children.append(
                        _mk_chunk(
                            doc.doc_id,
                            section_path,
                            btype,
                            [(page_index, block)],
                            doc.trust.publish_decision.value,
                            fp=fp,
                        )
                    )
                    continue
                if btype is ChunkType.list:
                    if run_type is not ChunkType.list:
                        _flush_run(
                            doc,
                            section_path,
                            run,
                            run_type or ChunkType.prose,
                            children,
                            fp=fp,
                        )
                        run, run_type = [], ChunkType.list
                    run.append((page_index, block))
                    continue
                # prose-ish: keep merging until the window limit hits
                if run_type is not ChunkType.prose:
                    _flush_run(
                        doc,
                        section_path,
                        run,
                        run_type or ChunkType.prose,
                        children,
                        fp=fp,
                    )
                    run, run_type = [], ChunkType.prose
                run.append((page_index, block))
                if sum(len(b.content) for _, b in run) >= CHILD_CHAR_LIMIT:
                    _flush_run(doc, section_path, run, ChunkType.prose, children, fp=fp)
                    run = []
            _flush_run(
                doc, section_path, run, run_type or ChunkType.prose, children, fp=fp
            )

            if not children:
                continue
            if len(children) > 1:
                parent = _mk_parent(
                    doc.doc_id,
                    section_path,
                    children,
                    doc.trust.publish_decision.value,
                    fp=fp,
                )
                chunks.append(parent)
                children = [
                    c.model_copy(update={"parent_chunk_id": parent.chunk_id})
                    for c in children
                ]
            chunks.extend(children)
        return chunks
