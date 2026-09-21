# RAG Subsystem — Scaling Investigation

Status: **investigation + agreed direction, not yet implemented.**
Decisions taken 2026-09-21: OCR scaling is out of scope (deployment
conversation, rag assumes a fed inbox); incremental is the *only*
pipeline — a full refresh is its degenerate case, triggered by a
pipeline fingerprint baked into `chunk_id`; enrich/model versions are
kept out of that fingerprint and swept over the corpus only after a
version is finalized on a sample. The numbers below are still
arithmetic, not measurements — see §"Validate before committing".

The scenario examined:

| Quantity | Value | Derived |
| --- | --- | --- |
| PDFs in library | 100,000 | ~1M pages at 10 pages/PDF |
| New PDFs per day | 1,000 | ~10k pages/day of OCR |
| Leaf chunks per 10-page doc | ~20–40 (structure-aware split) | **~3–5M chunks total** |
| New chunks per day | — | ~20k–40k |
| Avg chunk text | ~1KB | chunks body ~5GB, FTS index ~same again |

```text
                    where each stage breaks at 100k docs
 ┌──────────────────────────────────────────────────────────────────┐
 │ OCR (upstream)      ingest           publish         query       │
 │ 10k pages/day ───► glob 100k ────► full re-chunk ─► BM25 over    │
 │ backfill = weeks    files           + FTS wipe      3-5M rows    │
 │     ▲               + 1 row/doc     + full reindex      ▲        │
 │     │               per version         ▲               │        │
 │  real throughput    ────────────────────┘               │        │
 │  risk              CORE CONTRADICTION            fine as-is     │
 └──────────────────────────────────────────────────────────────────┘
```

## What survives the scale-up (keep designing around these)

- **The five protocol seams** (`01` §"The five protocol seams"):
  `CandidatePath` / `Shaper` / `Fuser` / `Assembler` / `Acquirer` know
  nothing about publish mechanics — the online path does not change when
  the offline path is rebuilt.
- **Query side**: SQLite FTS5 handles tens of millions of rows; the
  current BM25 SQL (`fts_path.py`) filters by `corpus_version` via a
  rowid join, and the FTS table holds only the active version — that
  shape stays valid. No query-side work is on the critical path.
- **Content-addressed `chunk_id`**: same text → same id. This is exactly
  the property incremental publishing needs (unchanged doc → unchanged
  chunk set → skip). M1 built it for rebuild-proof golden tests; it turns
  out to be the keystone of the scale design too.
- **`representations` registration** in snapshots: an unbackfilled
  projection stays invisible. At scale, backfills become long-running
  jobs; this "registered-or-invisible" rule is what keeps them safe.

## What breaks, ranked by severity

### 1. Publish is a full rebuild — the core contradiction

`pipeline.build()` on every run: `sorted(inbox.glob(...))` over *all*
bundles → re-chunk *everything* into one in-memory list →
`stage_chunks` row-by-row INSERT under a new `corpus_version` →
`publish` does `DELETE FROM chunks_fts` and re-inserts *every* chunk's
FTS row → **all inside one SQLite transaction**.

At 3–5M chunks:

- publish is tens of minutes to hours; the transaction holds a write
  lock and inflates the WAL to the size of the whole re-index;
  an interruption rolls back everything, with no resume point;
- memory holds the full chunk list before the first INSERT;
- `_write_corpus_projection` rewrites 100k markdown files per build.

The 1k-doc daily increment *cannot* be served by this path at all —
each daily publish costs as much as the historical backfill.

### 2. Version = full copy → history multiplies the corpus

`chunks` has `PRIMARY KEY (chunk_id, corpus_version)`: **every publish
duplicates every chunk's text** (5GB + per version, by design, for
rollback). Ten publishes ≈ 50M rows and tens of GB of duplicated text.
The rollback story ("old snapshots survive") does not survive contact
with 100k docs.

### 3. There is no "incremental" concept anywhere in the pipeline

Doc-level upsert exists (`upsert_document`), index-level does not:
changing or adding 1 document republishes 3–5M chunks. Related gap:
**there is no retraction path** — deleting a doc is implicit in the
full-rebuild model (absent from inbox = gone); an incremental model must
make it explicit (tombstone + manifest removal).

### 4. `canonical_json` in the documents table

The full annotation payload (entire `OcrDocument`) is stored per doc —
the schema comment already admits "playground scale". At 100k docs this
is plausibly the *largest single object* in the DB (10–30GB), while the
bundle on disk remains the real source. It should be a pointer plus
metadata, not a blob.

### 5. Enrich is all-or-nothing

`asyncio.run(enricher.enrich(chunks))` runs the LLM over everything the
build staged, with no checkpointing and no partial-failure semantics.
At 30k new chunks/day this is a continuous cost that must be resumable,
and enrich must be *degradable*: an enricher outage must not block
publishing the lexical view (chunks are searchable without inferred
fields today; the `representations` seam can carry "summary: absent").

### 6. M2 vectors force a storage decision

`embeddings` (BLOB + PK, no index) brute-force-scans. 3–5M × 1024-dim
float32 ≈ 12–20GB of vectors; a scan per query is out.

**Candidate comparison** (researched 2026-09-21):

| Option | Deployment | Index types | Scale | Maturity | Fit with current arch |
|---|---|---|---|---|---|
| **sqlite-vec** | 60KB extension, zero deps | IVF, DiskANN | 1–5M viable | pre-v1, breaking changes expected | highest (same SQLite DB) |
| **LanceDB** | embedded, Rust | IVF-PQ | 1M+ excellent | newer, stable | medium (separate file format) |
| **Postgres+pgvector** | PG server required | HNSW, IVF-Flat | 1M+ excellent | mature | medium (add adapter to `src/storage/`, update RAG imports) |
| **Dedicated vector DB** (Qdrant/Milvus) | independent service | HNSW, IVF, DiskANN | 1M+ excellent | mature | lowest (new ops system) |

**sqlite-vec details**: Mozilla-sponsored, used by OpenClaw/Hermes agents.
IVF and DiskANN indexes already supported (not brute-force only). Python API:
`import sqlite_vec; sqlite_vec.load(conn)`. Risk: pre-v1 with known API
instability (`01-design-rationale.md` §"Deviations" notes "2026 maintenance
churn").

**LanceDB details**: Columnar Lance format, disk-friendly (low RAM),
IVF-PQ indexing. Embedded like sqlite-vec but introduces a separate storage
format outside the SQLite ecosystem.

**Decision logic** (apply when M2 gate triggers):

```
rag eval exposes lexical misses
    │
    ├─ Validate first: 5k-doc synthetic corpus with vector search实测
    │
    ├─ Primary factor: QPS (queries per second)
    │   │
    │   ├─ QPS < 100 (single-user, CLI tool)?
    │   │   └─ YES → embedded solution (sqlite-vec or LanceDB)
    │   │        │
    │   │        ├─ Vector scale < 5M, accept pre-v1 risk?
    │   │        │   └─ YES → sqlite-vec (smallest diff, same DB)
    │   │        │        NO  → LanceDB (more stable, disk-friendly)
    │   │
    │   ├─ QPS 100–1000 (multi-user service)?
    │   │   └─ single-node Qdrant/Milvus (connection pooling, query optimization)
    │   │
    │   └─ QPS > 1000 (high-concurrency SaaS)?
    │       └─ distributed vector DB + replicas (load balancing, horizontal scaling)
    │
    ├─ Secondary factor: data volume
    │   └─ Only matters within the QPS tier:
    │       • < 5M vectors: any solution in the tier works
    │       • 5M–50M: need proper indexing (IVF/DiskANN, not brute-force)
    │       • > 50M: distributed sharding required
    │
    └─ Tertiary factor: operational complexity
        └─ Dedicated vector DB only if team has ops capacity (Kubernetes, monitoring)
```

**Why QPS is the core factor** (not data volume):

Data volume is the visible metric, but access pattern is the actual constraint.
A 100k-doc corpus with 1000 concurrent users needs Qdrant; a 1M-doc corpus
with 1 user needs sqlite-vec. The difference is connection management, query
parallelization, and caching — problems embedded solutions don't solve because
they don't exist at low QPS.

Concrete boundaries (empirical):
- **QPS < 100**: single SSD + OS page cache + IVF index → <50ms latency
- **QPS 100–1000**: single node saturates CPU/network → need dedicated query engine
- **QPS > 1000**: single node saturates → need distributed sharding

This project: single-user CLI tool, peak <10 QPS. sqlite-vec's single
connection + IVF index + OS page cache is sufficient. Dedicated vector DB
would solve problems that don't exist here.

**Recommended strategy**:

1. **Short-term (M2 initial)**: sqlite-vec — smallest diff, zero ops, IVF/DiskANN
   available. Mitigate pre-v1 risk with a `VectorStore` protocol abstraction
   (`src/rag/vector_store.py`: `add(chunk_id, vector)`, `search(vector, k)`)
   so the implementation can swap without touching the query engine.

2. **Mid-term fallback**: LanceDB — if sqlite-vec proves unstable or
   performance实测 at 5k-doc scale shows unacceptable QPS/P99 latency.
   The `VectorStore` abstraction makes this a new implementation, not a
   rewrite.

3. **Not recommended for this project**: Postgres+pgvector — the `src/storage/`
   layer already anticipates Postgres as a sibling adapter (`postgres.py`), so
   migration cost is "add adapter + update RAG imports", not "rewrite". But
   introducing a PG server just for vectors is over-engineering at <10 QPS;
   only choose this if PG infrastructure already exists for other reasons.

**Migration cost estimate** (if Postgres becomes necessary later):

The `src/storage/` abstraction separates data access from business logic, so
switching databases is a localized adapter change, not a rewrite:

- **Changes** (~100 lines): add `src/storage/postgres.py` (`PostgresClient`),
  add `src/storage/fts_postgres.py` (tsvector full-text search), update
  `src/rag/store.py` and `src/rag/fts_path.py` imports.
- **Unchanged** (~2000 lines): publish/manifest logic, chunking, assemble,
  answer, all business rules.

This is the payoff for the layered architecture: database selection is an
adapter-layer decision, not a business-logic rewrite. The same pattern applies
to any future database (MySQL, CockroachDB, etc.) — add a sibling module to
`src/storage/`, update the imports in the consuming code.

**Core principle**: Do not lock the storage before the corpus proves vectors
are needed (`00-overview` M2 gate). sqlite-vec's minimal footprint makes it
the safest "probe first" choice. The `VectorStore` abstraction is the exit
ramp that keeps options open.

### 7. Trust/review does not scale to 100k humans

`reviewed` + hand-proofreading every risky bundle was never going to
survive; at this size the trust model becomes *sampled* QC plus
threshold policy on `publish_decision`, not per-doc human review.

### 8. Upstream OCR is the real throughput wall (declared out of scope)

1k PDFs/day = 10k pages/day through PaddleOCR-VL, and the 100k-doc
historical backfill is a multi-week batch job. Decided 2026-09-21: OCR
scaling (vLLM servers, GPU fleet, backfill scheduling) is a deployment
conversation outside `src/rag` — this document assumes the bundle inbox
gets fed. The one contract rag keeps relying on: a re-OCR that changed
content must produce a new `source_sha256`, because the incremental
design below treats "same sha" as "nothing to do".

## The redesign hinge: version as manifest, not as copy

Everything in §1–§3 collapses to one change. Replace "a version owns
rows" with "a version *lists* chunk ids":

```text
 today:  chunks(chunk_id, corpus_version) PK   ── every publish copies all text
         publish = wipe FTS, re-insert all, flip pointer   (hours, 1 big txn)

 target: chunks(chunk_id) PK, immutable, content-addressed  ── one row per text, ever
         chunks_fts rowid = chunks.rowid, upserted per chunk, never wiped
         snapshot_chunks(corpus_version, chunk_id)   ── the manifest
         publish = insert this batch's new chunks + manifest rows + flip pointer
         rollback = point at the old manifest        ── same guarantee, zero copy
```

The publish transaction shrinks to the daily increment (~30–40k chunk
manifest rows + only genuinely-new chunk texts), i.e. seconds, and
old-version GC becomes a janitor query ("chunk ids in no live
manifest") instead of a storage tax paid on every publish.

**Incremental is the only path; a full rebuild is its degenerate case.**
Agreed 2026-09-21. Bake a **pipeline fingerprint** (hash of chunker
version, section rules — everything that affects chunk boundaries) into
`chunk_id` derivation, next to the content hash:

- settings unchanged → new docs produce new ids, old docs diff to empty
  and are skipped → the daily 1k only processes 1k;
- settings changed → every chunk id changes → the *same* incremental
  loop "discovers" 3–5M new chunks and the full refresh happens as a
  resumable batch, with no separate `rag rebuild` command to forget;
- "when should we reprocess everything" stops being tribal knowledge and
  becomes a query the diff itself answers.

What must **not** go into the fingerprint: enrich prompts and embedding
models. Those are side projections keyed `(chunk_id, provider_version)`
(like `embeddings` already is), so iterating a prompt never re-chunks
the library — see §"Full refresh is a planned migration" for how the
industry handles the cost question that leaves.

Derived work, in dependency order:

1. **Split `rag build` into `rag ingest` + `rag publish`.** Ingest scans
   the inbox, skips bundles already seen (doc table + sha), chunks only
   new/changed docs, and stages them; resumable per-bundle, a broken
   bundle lands in a dead-letter list (already half-there: `build`
   collects `report["errors"]` and continues). Publish runs the manifest
   swap above.
2. **De-version `chunks`, add `snapshot_chunks`** (the schema surgery
   above). `stage_chunks` becomes idempotent upsert-by-content-hash;
   `chunk_ids_matching`'s full-table `LIKE '%..%'` scan stops being a
   table-wide monster because the table no longer multiplies.
3. **FTS incremental upsert**: drop the wipe-and-reindex loop in
   `publish()`; `FtsTable.upsert` already supports per-rowid writes —
   index only chunks new to this manifest.
4. **Move `canonical_json` out of the DB** (keep `source_path` + sha as
   the pointer; the bundle on disk is the source of truth, and
   `corpus/*.md` projections remain the greppable insurance).
5. **Explicit retraction**: doc-level tombstone; publish removes its
   chunks from the new manifest.
6. **Ingest-side queue discipline**: single inbox directory with 100k
   files is itself a problem (glob + sorted in memory) — shard by date
   or record cursor state in the DB; either way the scan must be
   incremental, not a full listing per run.
7. **Enrich as a background projection** with checkpointing, feeding
   the `representations` field per projection rather than a global
   flag — memoized on `(chunk_id, prompt_version, model)`, so a
   finalized prompt version backfills the whole corpus exactly once and
   iterating a version never touches already-derived rows.

Deliberately *not* on this list: any query-side rewrite, any vector
store decision, anything about `src/storage`. The online path and the
seams were built for this; the offline path was not.

## Full refresh is a planned migration, not an accident

Surveyed 2026-09-21 (sources inline); the industry
converges on four points, all compatible with the hinge above:

1. **Model/boundary changes genuinely do force 100% re-derivation** —
   vectors from two models can't share an index, so nobody tries to
   avoid it. Instead they treat it as a *data migration*: build a shadow
   index in parallel, dual-write during backfill, read-shadow to verify,
   flip an alias, and keep the old index for a rollback window
   ([formation.dev](https://formation.dev/blog/embedding-model-upgrade-migration);
   [Azure's migration answer](https://learn.microsoft.com/en-in/answers/questions/5912134/how-to-migrate-the-azure-search-index-with-latest)
   is the same shape). Our manifest + pointer flip *is* the alias;
   retained snapshots *are* the rollback window.
2. **Change detection by hash, throughput by async batching, cost by
   tiered models** — cheap models do enrichment-grade work
   ([extend.ai](https://www.extend.ai/resources/batch-document-ingestion-rag-knowledge-bases)).
   Our fingerprint-in-chunk_id is point 1 made structural; enrich should
   use the cheapest model that passes eval, and the LLM Batch-style
   half-price async path where the provider has one.
3. **No periodic re-embedding** — the standing advice is that re-index
   only when model/chunking actually changed
   ([OpenAI community](https://community.openai.com/t/do-i-need-to-re-index-my-embedding-database-periodically/973805)).
   Fingerprints give changes a precise trigger; "maintenance full
   rebuild" is an anti-pattern here.
4. **Iterate before you sweep** (agreed 2026-09-21): a new enrich prompt
   is proven on a sample against `rag eval` *before* its version is
   declared final; only final versions get corpus-wide backfill.
   Combined with (chunk_id, provider_version) memoization, the expensive
   full sweep happens once per *decision*, not once per *draft*.

## Validate before committing

The numbers above are arithmetic, not measurements. Before cutting the
schema:

- generate a synthetic corpus of ~5k bundles (10 pages each, so the
  per-doc cost is real even if the corpus is 5% of target), run
  `rag build`, and record: wall time, `kb.db` size, peak RSS, and how
  much of the time is `stage_chunks` vs `publish` vs the markdown
  projection;
- extrapolate to 100k/20x and compare against the "hours / tens of GB"
  claims in §1–§2 — if SQLite's constants surprise us (e.g. staged FTS
  insert rate), some severity ordering above changes;
- the experiment doubles as the first scale regression: the same script
  after the manifest redesign proves the before/after delta that
  justifies the surgery.

Not yet done: nothing in this document has been executed — no synthetic
corpus, no timings. Treat every number here as an estimate until the
validation run happens.
