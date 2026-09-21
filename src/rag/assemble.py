"""Context assembly: the last offline-contract-respecting step.

Takes fused candidates (rank order = anchor order), loads the child chunks,
and expands each hit to its section parent for scope/exception recovery —
parents enter the prompt as context but are not citable, because their
text is the union of children (citing them would double-count).
"""

from __future__ import annotations

from sqlalchemy import text

from .contract import Candidate, Chunk, EvidencePack
from .store import row_to_chunk

_MAX_PACK_CHUNKS = 12

# BM25 grows with real match quality; on this repo's corpora a genuine hit
# scored three orders of magnitude above a single stray token surviving the
# graded OR relaxation. Below this floor the "evidence" is noise, so the
# pack is marked insufficient and the answerer is never asked to
# compensate for missing evidence (design doc §online, CH03_04 thin
# generation).
_WEAK_EVIDENCE_FLOOR = 1e-3


def _fetch_by_ids(
    client, version: int, ids: list[str]
) -> dict[str, Chunk]:  # noqa: ANN001
    if not ids:
        return {}
    placeholders = ", ".join(f":i{n}" for n in range(len(ids)))
    params: dict = {f"i{n}": cid for n, cid in enumerate(ids)}
    params["v"] = version
    with client.session() as db:
        rows = (
            db.execute(
                text(
                    # placeholders generated, ids are bound params
                    f"SELECT * FROM chunks WHERE corpus_version = :v "  # nosec B608
                    f"AND chunk_id IN ({placeholders})"
                ),
                params,
            )
            .mappings()
            .all()
        )
    return {r["chunk_id"]: row_to_chunk(r) for r in rows}


def assemble(
    client,  # noqa: ANN001 - SqliteClient
    version: int,
    query: str,
    fused: list[Candidate],
    k: int,
    path_meta: list[dict],
    *,
    expand_parents: bool = True,
) -> EvidencePack:
    picked = [c for c in fused if "rrf" in c.ranks][:k]
    children = _fetch_by_ids(client, version, [c.chunk_id for c in picked])
    pack_chunks = [children[c.chunk_id] for c in picked if c.chunk_id in children]

    strength: dict = {
        "n_candidates": len(picked),
        "best_score": max((c.scores.get("fts", 0.0) for c in picked), default=None),
        "paths": path_meta,
    }
    if not pack_chunks:
        return EvidencePack(
            query=query,
            strength=strength,
            insufficient=True,
            notes=["no candidates matched the active snapshot"],
        )
    if (
        strength["best_score"] is not None
        and strength["best_score"] < _WEAK_EVIDENCE_FLOOR
    ):
        return EvidencePack(
            query=query,
            chunks=pack_chunks,
            strength=strength,
            insufficient=True,
            notes=[
                f"best score {strength['best_score']:.2e} below the weak-evidence floor"
            ],
        )

    parents: list[Chunk] = []
    if expand_parents:
        parent_ids = [c.parent_chunk_id for c in pack_chunks if c.parent_chunk_id]
        fetched = _fetch_by_ids(client, version, parent_ids)
        seen: set[str] = set()
        for pid in parent_ids:
            if pid in fetched and pid not in seen:
                seen.add(pid)
                parents.append(fetched[pid])
        parents = parents[:_MAX_PACK_CHUNKS]

    return EvidencePack(
        query=query, chunks=pack_chunks, parents=parents, strength=strength
    )
