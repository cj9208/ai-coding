"""The online retrieval engine: shaper -> registered paths -> fuse -> assemble.

Which paths run is decided by ``snapshots.representations`` — a projection
that exists but was never backfilled is invisible, not half-effective.
M1 registers exactly one path (fts); adding a second is one entry in
``build_paths`` plus the backfill that flips its representation string.
"""

from __future__ import annotations

from storage import SqliteCache, sha256_hex

from .assemble import assemble
from .contract import EvidencePack
from .fts_path import Fts5Path
from .fuse import rrf_fuse
from .protocols import CandidatePath, ShapedQuery
from .shape import LexicalShaper
from .store import RagStore
from .tracing import NO_TRACER, TracedPath, TracedShaper, Tracer


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
    store: RagStore,
    question: str,
    k: int = 5,
    shaped: ShapedQuery | None = None,
    tracer: Tracer | None = None,
    cache: SqliteCache | None = None,
) -> EvidencePack:
    tracer = tracer or NO_TRACER
    with tracer.span(
        "rag.retrieve", question=question[:120], **{"gen_ai.retrieval.top_k": k}
    ) as rsp:
        version = store.active_version()
        if version is None:
            pack = empty_pack(question, "no published snapshot — run `rag build` first")
        elif not (paths := build_paths(store, version)):
            pack = empty_pack(question, "active snapshot has no live representations")
        else:
            cache_key = sha256_hex(f"{question}:{k}")
            cache_tag = str(version)
            if cache is not None:
                cached = cache.get(cache_key, tag=cache_tag)
                if cached is not None:
                    pack = EvidencePack.model_validate_json(cached)
                    rsp.set(
                        cache_hit=True,
                        snapshot_version=version,
                        n_candidates=pack.strength.get("n_candidates", 0),
                        insufficient=pack.insufficient,
                    )
                    return pack
            shaped = shaped or TracedShaper(LexicalShaper(), tracer).shape(question)
            outcomes = [
                (p.name, TracedPath(p, tracer).search(shaped, k * 2)) for p in paths
            ]
            fused = rrf_fuse([(name, out.candidates) for name, out in outcomes])
            pack = assemble(
                store.client,
                question,
                fused,
                k,
                path_meta=[out.meta for _, out in outcomes],
            )
            if cache is not None:
                cache.put(cache_key, pack.model_dump_json(), tag=cache_tag)
        rsp.set(
            snapshot_version=version,
            n_candidates=pack.strength.get("n_candidates", 0),
            insufficient=pack.insufficient,
        )
    return pack
