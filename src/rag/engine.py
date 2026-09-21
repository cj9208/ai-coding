"""The online retrieval engine: shaper -> registered paths -> fuse -> assemble.

Which paths run is decided by ``snapshots.representations`` — a projection
that exists but was never backfilled is invisible, not half-effective.
M1 registers exactly one path (fts); adding a second is one entry in
``build_paths`` plus the backfill that flips its representation string.
"""

from __future__ import annotations

from .assemble import assemble
from .contract import EvidencePack
from .fts_path import Fts5Path
from .fuse import rrf_fuse
from .protocols import CandidatePath, ShapedQuery
from .shape import LexicalShaper
from .store import RagStore


def build_paths(store: RagStore, version: int) -> list[CandidatePath]:
    snapshot = store.snapshot(version) or {}
    representations = snapshot.get("representations", {})
    paths: list[CandidatePath] = []
    if representations.get("fts", "").startswith("ready"):
        paths.append(Fts5Path(store.client, version))
    # M2+: elif representations.get("vector") == f"ready@{model}": DensePath(...)
    return paths


def empty_pack(query: str, note: str) -> EvidencePack:
    return EvidencePack(
        query=query, strength={"n_candidates": 0}, insufficient=True, notes=[note]
    )


def retrieve(
    store: RagStore, question: str, k: int = 5, shaped: ShapedQuery | None = None
) -> EvidencePack:
    version = store.active_version()
    if version is None:
        return empty_pack(question, "no published snapshot — run `rag build` first")
    paths = build_paths(store, version)
    if not paths:
        return empty_pack(question, "active snapshot has no live representations")

    shaped = shaped or LexicalShaper().shape(question)
    outcomes = [(p.name, p.search(shaped, k * 2)) for p in paths]
    fused = rrf_fuse([(name, out.candidates) for name, out in outcomes])
    return assemble(
        store.client,
        version,
        question,
        fused,
        k,
        path_meta=[out.meta for _, out in outcomes],
    )
