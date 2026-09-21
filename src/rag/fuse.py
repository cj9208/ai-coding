"""Reciprocal Rank Fusion — written and tested while only one path exists.

Why fuse with one input (identity) rather than wait for M2: fusion is the
seam where paths must agree, and CH03's RRF choice is deterministic enough
to pin down now. Dedupe on chunk_id is the behaviour that matters even
today (a chunk can match on several token groups).
"""

from __future__ import annotations

from .contract import Candidate

RRF_K = 60


def rrf_fuse(outcomes: list[tuple[str, list[Candidate]]]) -> list[Candidate]:
    """Merge per-path ranked lists; fused score stored under ``"rrf"``."""
    merged: dict[str, Candidate] = {}
    totals: dict[str, float] = {}
    for path_name, candidates in outcomes:
        for cand in candidates:
            rank = cand.ranks.get(path_name)
            if rank is None:
                continue
            slot = merged.setdefault(cand.chunk_id, cand.model_copy(deep=True))
            slot.scores.update(cand.scores)
            slot.ranks.update(cand.ranks)
            totals[cand.chunk_id] = totals.get(cand.chunk_id, 0.0) + 1.0 / (
                RRF_K + rank
            )
    fused = sorted(totals, key=lambda cid: -totals[cid])
    result: list[Candidate] = []
    for rank, cid in enumerate(fused, start=1):
        cand = merged[cid]
        cand.scores["rrf"] = totals[cid]
        cand.ranks["rrf"] = rank
        result.append(cand)
    return result
