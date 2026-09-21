# RAG Subsystem — Incremental Design: Manifest + Fingerprint

**One sentence:** replace "a version owns rows" with "a version lists
(doc_id, pipeline_fp) pairs" — chunks become one immutable,
fingerprint-scoped table, publish becomes a bounded diff transaction,
and a settings change is carried by the same loop that ingests today's
1k docs.

```text
status: design of record, pre-implementation (agreed 2026-09-21)
        prerequisites read: 04-scaling.md (what breaks, why manifest)
        touches shipped code: store / pipeline / fts_path / assemble / cli
```

## Three invariants this design exists to protect

1. **Retrieval never sees a half-built index** (CH03_02, kept from M1):
   publish is still ONE transaction; only its size changed — from the
   whole corpus to this batch's diff.
2. **One row per derived object, ever**: a chunk's text lives exactly once
   in `chunks`, keyed by its id; history is a set of manifests, not a
   stack of copies.
3. **Cheap things churn, expensive things don't**: chunking is cheap
   enough that a full re-chunk is a normal batch; LLM enrich and
   embeddings are expensive, so *nothing they depend on* is inside the
   fingerprint that triggers re-chunking.

## The pipeline fingerprint

`pipeline_fp` = short hash of a declared, code-visible table — the same
posture as `sources.UPSTREAMS` pins:

```python
# src/rag/versions.py (new)
PIPELINE: dict[str, Any] = {
    "chunker": "structure_aware@v1",   # ChunkStrategy.name + its rule version
    "child_char_limit": 800,
    "structure": "v1",                 # reading_order / FURNITURE_KINDS rules
    "id_format": "v2",                 # chunk_id derivation format version
}

def pipeline_fp() -> str:
    return sha256_hex(json.dumps(PIPELINE, sort_keys=True), length=8)
```

and baked into id derivation (the one contract change):

```python
# contract.py — Chunk.chunk_id_for gains an fp parameter
sha256_hex("|".join([fp, doc_id, section_path, *block_keys]), length=12)
```

**In the fingerprint** — anything that changes chunk *boundaries or
identity*: chunker rules, window limits, section attribution, the id
format itself.
**Out of the fingerprint** — anything that is a rebuildable projection
over unchanged chunks: enrich prompt (→ `inferred.prompt_ver`),
embedding model (→ `embeddings.model_id`), BM25 weights and the CJK
fold (→ the `representations` version string, e.g. `ready@fold-v2+w5`).
Consequences, stated plainly:

- settings unchanged, new bundle → only that doc's chunks are new;
- settings changed → every id changes → the *same* ingest loop produces
  a corpus-wide diff; the full refresh is the degenerate case, not a
  mode. No `rag rebuild` command exists.
- prompt iteration never re-chunks anything.

## Schema v2

Diff from the shipped five tables (v1 → v2), then the DDL:

| Change | Why |
| --- | --- |
| `chunks`: drop `corpus_version` from PK → `chunk_id` alone | one row per (id) ever; id already contains fp + doc + position |
| `chunks`: add `pipeline_fp` column + `(doc_id, pipeline_fp)` index | the manifest joins on it; GC queries it |
| `chunks`: drop `inferred` column → new `inferred` table | invariant 3: enrich version must not touch the chunk row (04 §5) |
| new `snapshot_docs` | the manifest: what a version actually *is* |
| `documents`: drop `canonical_json`, add `ingest_state` + `chunked_with_fp` + `chunk_count` | 04 §4 (bundle on disk is the source); ingest cursor and retraction live here |
| `embeddings`, `snapshots`, `meta` | unchanged (embeddings is already `(chunk_id, model_id)`-keyed — the right shape) |

```sql
CREATE TABLE documents (
    doc_id           TEXT PRIMARY KEY,       -- = make_doc_id(source.sha256)
    source_sha256    TEXT NOT NULL,
    source_path      TEXT NOT NULL,
    extractor        TEXT NOT NULL,
    reviewed         INTEGER NOT NULL DEFAULT 0,
    publish_decision TEXT NOT NULL,
    risk_flags       TEXT NOT NULL DEFAULT '[]',
    quality          TEXT NOT NULL DEFAULT '{}',
    page_count       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL DEFAULT '',
    -- staged: chunked, waiting for the next publish
    -- live:   in the active manifest
    -- retracted: will be absent from the next manifest (04 §3, explicit now)
    ingest_state     TEXT NOT NULL DEFAULT 'staged',
    chunked_with_fp  TEXT,                   -- NULL = never chunked; != pipeline_fp() = stale
    chunk_count      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE chunks (
    chunk_id           TEXT PRIMARY KEY,     -- sha12(fp|doc_id|section_path|block_keys)
    pipeline_fp        TEXT NOT NULL,
    doc_id             TEXT NOT NULL REFERENCES documents(doc_id),
    chunk_type         TEXT NOT NULL,
    is_parent          INTEGER NOT NULL,
    parent_chunk_id    TEXT,
    section_path       TEXT NOT NULL DEFAULT '',
    page_start         INTEGER NOT NULL,
    page_end           INTEGER NOT NULL,
    block_keys         TEXT NOT NULL,
    text               TEXT NOT NULL,
    structured_payload TEXT,
    trust_level        TEXT NOT NULL DEFAULT 'pass',
    content_hash       TEXT NOT NULL         -- audit only: doc_id is sha-addressed, so same id ⇒ same text by construction
);
CREATE INDEX chunks_doc_fp ON chunks(doc_id, pipeline_fp);

CREATE TABLE snapshot_docs (                  -- the manifest, doc-level
    corpus_version INTEGER NOT NULL,
    doc_id         TEXT NOT NULL,
    pipeline_fp    TEXT NOT NULL,
    PRIMARY KEY (corpus_version, doc_id)
);
CREATE INDEX snapshot_docs_doc ON snapshot_docs(doc_id);

CREATE TABLE inferred (                       -- enrich as a side projection
    chunk_id   TEXT NOT NULL,
    prompt_ver TEXT NOT NULL,                 -- versions.PROMPT_VER
    title      TEXT NOT NULL DEFAULT '',
    keywords   TEXT NOT NULL DEFAULT '',      -- space-joined, FTS-ready
    summary    TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    PRIMARY KEY (chunk_id, prompt_ver)
);
```

### Why the manifest is doc-level, not chunk-level

A doc's chunk set is a pure function of `(bundle sha, pipeline_fp)` —
chunking is deterministic. So
`chunks_in(version) ≡ chunks JOIN snapshot_docs ON (doc_id, fp)` and a
version can be *listed in 100k rows instead of 5M*: publish copies the
previous manifest (≈100k inserts, ~1s in SQLite), FTS diff and GC both
resolve through the join. A chunk-level manifest would buy nothing and
multiply storage by retained snapshot count. If publishing frequency
ever outruns the full-copy, the fix is a delta chain
(`snapshot_docs(parent_version, added, removed)`) — deliberately not
designed here, because daily publish × 100k rows is years of headroom.

**Caveat the join makes honest:** `chunk_id` pins `block_keys`, not text
— same bundle + same fp ⇒ same ids ⇒ same texts, which is exactly the
determinism the doc-level manifest leans on. It also means a *reviewed*
bundle (text fixed via `review.json`, block ids unchanged) keeps its
ids — handled in the ingest step: a changed source sha yields a new
`doc_id`, so the old generation is simply retracted, not patched in
place.

## Publish: the transaction, sized

```text
1  begin
2  base  = current active manifest               (SELECT doc_id, fp FROM snapshot_docs WHERE v = active)
3  insert base rows for v_new                    (~100k rows)
4  +  rows for documents.ingest_state = 'staged' AND chunked_with_fp = pipeline_fp()
5  −  rows for ingest_state = 'retracted'
6  FTS diff:
7     upsert   chunks of newly listed (doc, fp) pairs            (per-chunk FtsTable.upsert, never a wipe)
8     delete   chunks of retracted/replaced pairs still indexed
9  snapshot row (counts + representations) ; meta.active_version = v_new
10 commit
```

Daily increment ⇒ step 4 touches 1k rows, step 7 touches ~30k FTS
upserts — seconds, one transaction, invariant 1 intact. Full refresh
(fp change) ⇒ the same code, 3–5M upserts; that's the planned
migration of 04's last section, and it is resumable *between* publishes,
not mid-transaction: ingest can run for days staging docs, and the
corpus keeps serving the old manifest until one publish flips it.

`representations` at publish time, per projection:
`{"fts": "ready@fold-v2+w5", "inferred": "ready@prompt-v2" | "partial@prompt-v3", "vector": "absent@bge-m3"}` —
`partial` is new and load-bearing: an enrich backfill in progress must
register as "live but incomplete, do not rely on summary field" instead
of the current all-or-nothing boolean.

## What each old store function becomes

| v1 (`store.py`) | v2 |
| --- | --- |
| `next_version` + `stage_chunks(version, chunks)` | `stage_document(doc, chunks, fp)`: INSERT chunks `ON CONFLICT(chunk_id) DO NOTHING`, flip `documents.ingest_state='staged'`, set `chunked_with_fp` |
| `publish` (wipe FTS, reindex all) | `publish_diff` (the 10 steps above) |
| `chunks_in(version)` | `chunks JOIN snapshot_docs` — or, for the daily path, not needed at all |
| `chunk_ids_matching` (`LIKE '%..%'` over a multiplying table) | same predicate over a table that no longer multiplies — eval-only, fine |
| `upsert_document` (writes `canonical_json`) | drops the blob column write; `source_path` + sha is the pointer (04 §4) |

Query side changes are one join (`fts_path._SQL`: replace
`c.corpus_version = :v` with the `snapshot_docs` join) and
`assemble._fetch_by_ids` (chunk_id is globally unique → fetch by id;
hydrate `inferred` from the active `prompt_ver`). The EvidencePack
contract, the five seams, and the online pipeline are untouched — as
promised in 04.

## CLI shape

```text
rag ingest [--inbox DIR] [--limit N]   # scan, skip (sha,fp)-seen, chunk, stage; resumable, dead-letter on error
rag publish                            # the diff transaction; prints diff sizes before/after
rag retract <doc_id>                   # ingest_state='retracted'; takes effect at next publish
rag build --inbox DIR                  # kept as: ingest then publish, for toy corpora and tests — a compose, not a code path
rag gc [--keep N]                      # delete chunks/inferred/embeddings referenced by no retained snapshot; prune snapshot_docs older than N
rag status                             # + backlog (staged/retracted counts), fp, chunk ids whose fp != active manifest's
```

`rag build` surviving as a two-line compose is deliberate: tests and the
demo keep one command; production cron runs `ingest` continuously and
`publish` on a schedule (or on backlog threshold).

## Migration from v1

No in-place ALTER path. The live corpus is the one-doc demo
(verify_doc); v2 is validated by the synthetic-fixture tests and the 5k
measurement run against a fresh `kb.db`. Old `data/rag/kb.db` is
disposable by the same argument that made FTS rebuildable — the bundles
are the source of truth. This concession is only cheap *because* the
corpus is still toy-sized; it is off the table after the first real
backfill, which is why it is recorded as a decision here rather than
left to whoever migrates.

## Test plan (mirrors source layout)

- `test_store.py`: publish_diff size invariance — 2nd publish with no
  staged docs writes 0 FTS rows; staged doc moves exactly its own chunks
  live; retraction removes them.
- `test_versions.py`: fp is stable under key reordering, changes iff a
  boundary-affecting value changes; prompt/model bumps leave fp alone.
- `test_chunking.py`: ids change when fp changes (id_format pin).
- `test_pipeline.py`: ingest skip-on-(sha,fp); a re-run after touching
  nothing stages 0 docs; build == ingest+publish equivalence.
- `test_fts_path.py` / `test_assemble.py`: the join returns the same
  candidates v1's version column did, over a corpus with two fp values
  coexisting (the rollback scenario).

## Open questions (resolved)

1. **Snapshot retention**: keep N full manifests vs. delta chain — N
   full manifests assumed above; revisit at first ops data.
   **→ Deferred.** 数据量小（100k doc × daily publish），几年内不需要
   delta chain；等运维数据出来再定保留策略。
2. **`inferred` hydration at assembly** — join-time lookup (extra query
   per assemble) vs. FTS-covering columns; decided by the 5k run's
   profile.
   **→ Deferred, config-controlled.** 暂不考虑；后期通过 config 控制
   哪些 retrieval 可以启用、加多少，瓶颈在参数调节和硬件资源。
3. **Publish trigger** for the daily path: fixed schedule vs. backlog
   threshold vs. both; matters once ocr-review ships corrections
   mid-day.
   **→ Deferred, easy.** 后期实际运行时再定——cron job 或 event-driven
   （monitor 特定位置是否有数据导入），不是当前重点。
4. **Enrich partiality and eval**: does `rag eval` score the lexical
   path over `partial@v3` or pin `ready@v2` until the sweep finishes —
   an eval-semantics question the golden set's next revision must
   answer.
   **→ Wait for batch to finish.** partial eval 意义不大；等 batch
   更新完成后再跑 eval。
