# RAG Subsystem — Design Rationale

Status: **M1 implemented** (lexical-only vertical slice). The corpus is not
chosen yet, so this design's job is to make the *pipeline* a fact and every
*retrieval technology* a plug-in — see "Why plugins with one implementation".
This document answers *why the design is shaped this way*; for what actually
shipped (module map, constants, deviations from this text) read
`02-implementation.md`, for how to run it `03-usage.md`.

Reference: the layer model comes from the blog note set
`AI_study/rag-orchestration-architecture` (CH03_01–04). This document records
where we follow it and where we deliberately deviate.

## Big picture

```text
                 (already built)                    (this package: src/rag)
 ┌───────────────────────────────────┐   ┌───────────────────────────────────────────┐
 │ pdf ── ocr-backend parse ──┐      │   │  OFFLINE  (rag build — deterministic)     │
 │        [ocr-review proofread]     │   │                                           │
 │        apply_review ─────────┘    │   │  Acquirer ─► Structure ─► Validate        │
 │        = OcrDocument v1.0         │   │      │         (section_path)  │ pass/    │
 └────────────────┬──────────────────┘   │      ▼           ▼           ▼ quarantine│
                  │  *.ocr.json          │  CanonicalDoc ─► Chunk[] ─► Index ─► Snapshot
                  ▼                      │  (content-addressed chunk ids)   (atomic) │
          data/ocr_backend/out ─────────►│                                          │
                                         ├───────────────────────────────────────────┤
                                         │  ONLINE  (rag query)                      │
                                         │  Shape ─► CandidatePath(s) ─► Fuse ─►     │
                                         │  Assemble ─► EvidencePack ─► Answer(5)    │
                                         └───────────────────────────────────────────┘
```

The two halves meet at a published snapshot, exactly as CH03 prescribes:
the offline side decides what can be found, the online side decides what is
used, and neither compensates for the other's defects.

## The spine is contracts, not stages

Six data objects cross the pipeline. They are frozen for M1; adding fields
later is compatible, changing shape is a redesign.

```text
OcrDocument ──► CanonicalDoc ──► Chunk[] ──► Candidate[] ──► EvidencePack ──► Answer
  (existing)     +lineage          the truth    any retrieval      cited         5 outcomes
                 +section_path                 path speaks        context
                 +trust                                           the same way
```

| Object | Key fields | Why this shape |
| --- | --- | --- |
| `OcrDocument` | unchanged, embedded as-is | it already *is* CH03_01's canonical contract (blocks=kinds+bbox+scores, table HTML kept structured); re-defining one would be ceremony |
| `CanonicalDoc` | `doc_id`, `source` (sha256 lineage, `reviewed` flag), embedded `document`, `section_paths["page:block"]`, `trust` (decision + risk flags + metrics) | structure reconstruction and validation produce annotations *beside* the contract, never inside it — same sidecar philosophy as ocr-review |
| `Chunk` | `chunk_id` (content-addressed), `doc_id`, `parent_chunk_id`, `chunk_type`, `section_path`, `text` (markdown rendering of its blocks), `structured_payload`, `page_span`, `block_keys`, `content_hash`, `trust_level`, `inferred` (LLM title/keywords/summary, separate dict) | the single truth table. `inferred` apart from source text = CH03_02's "authoritative vs inferred" rule. Markdown is a *rendering of blocks*, not a lossy intermediate file we re-parse |
| `Candidate` | `chunk_id`, `scores: {path: float}`, `ranks: {path: int}` | the plug-in pivot: every retrieval path (FTS now, vector/grep later) emits `Candidate`, so fusion/assembly/answering never change when a path is added |
| `EvidencePack` | ordered chunks, citation anchors `[1..n]`, `strength` (hit count + best score profile) | `strength` is the abstention input CH03_04 demands — insufficiency is decided by retrieval numbers, not by the generator's mood |
| `Answer` | `outcome ∈ {answered, partial, clarify, insufficient, escalated}`, `claims[{text, refs}]` | claims carry citation refs so integrity is checkable *after* generation |

## Storage: one SQLite file, five tables

`data/rag/kb.db` (REPO_ROOT-anchored, `--data-dir` overrides). All schema
knowledge lives in `src/storage` conventions: WAL/PRAGMA via
`storage.SqliteClient`, FTS5 with `fold_cjk` via `storage.FtsTable`.

```sql
documents(doc_id PK, source_sha256, source_path, extractor, reviewed,
          publish_decision, trust_level, risk_flags, quality, canonical_json)
chunks(chunk_id PK, doc_rowid, corpus_version, chunk_type, is_parent,
       parent_chunk_id, section_path, page_start, page_end, block_keys,
       text, structured_payload, trust_level, inferred, content_hash)
chunks_fts(title, body, section_path, keywords, summary)   -- FtsTable, rowid = chunks.rowid
embeddings(chunk_id, model_id, dim, vector, embedded_at)   -- empty in M1, see below
snapshots(corpus_version PK, published_at, doc_count, chunk_count,
          representations)                                  -- {"fts":"ready","vector":"absent@bge-m3"}
```

Three deliberate calls:

1. **The chunk table is the only truth; every index is a rebuildable
   projection.** FTS content is derived from chunks; the (empty) embeddings
   table would be derived the same way. Nothing downstream may assume a
   projection exists — `snapshots.representations` is the authoritative
   statement of which retrieval paths are live, and the query engine only
   runs registered ones. This is what "adding embedding later = one separate
   backfill job + one new CandidatePath file" mechanically means.
2. **chunk_id is content-addressed**:
   `sha256(doc_id | section_path | block_keys)[:12]`. Ids must survive a
   rebuild when text is unchanged, or future embedding rows silently orphan
   (the whole plug-in model bets on this key space). `content_hash` lets a
   backfill diff which chunks actually moved.
3. **Publish = one transaction**: stage chunks under a new `corpus_version`,
   then atomically (same `BEGIN IMMEDIATE`) replace the FTS rows with the
   new version's chunks and record the snapshot. Readers never see a
   half-built index — CH03_02's "publish only coherent snapshots", achieved
   with SQLite's own atomicity instead of a staging dance.

## The five protocol seams (each with exactly one implementation)

```python
Acquirer      .fetch(bundle_paths) -> list[CanonicalDoc]      # OcrBundleAcquirer
ChunkStrategy .split(doc) -> list[Chunk]                      # StructureAwareChunker
QueryShaper   .shape(query) -> ShapedQuery(tokens)            # LexicalShaper (deterministic)
CandidatePath .search(shaped, k) -> list[Candidate]           # Fts5Path  (+ name attr)
EnrichProvider.enrich(chunks) -> None (fills chunk.inferred)  # LlmEnricher (opt-in --enrich)
```

No second implementation is pre-written. An interface validated only by one
implementation is a guess, and internal code has no compatibility burden —
if the vector path needs the protocol bent, bending it is cheaper than
maintaining a dead adapter.

## Why lexical-only is a legitimate M1, not a placeholder

CH03_03 lists vector-only as a failure mode; lexical-only is not on that
list. Supporting evidence at design time (2026-09): BM25 holds or wins on
identifier-heavy and table-heavy lookup, which is what a business-document
corpus mostly asks; the classic agent-stack results that rehabilitated grep
were on *code* corpora, so our raw ripgrep route stays available (publish
writes `data/rag/corpus/<doc_id>.md` projections for it) but is not the
default engine for CJK prose. What lexical genuinely loses — vocabulary
mismatch on fuzzy queries — is attacked first without vectors:
LLM enrichment text (titles/keywords/summaries) enters the FTS row, and the
shaper can expand keywords via `llm_client`. The judge of whether vectors
are finally needed is `rag eval`, not preference.

## Online pipeline (M1 shape)

```text
query ─► LexicalShaper ─► Fts5Path (bm25, field-weighted, graded AND→OR relaxation)
      ─► fuse (RRF; identity today, multi-path tomorrow) ─► dedupe
      ─► parent expansion (child hit brings its section parent along)
      ─► EvidencePack + strength gate ─► llm_client.chat_json (thin generation)
      ─► deterministic citation check: every claim ref ∈ anchors, else downgrade
      ─► Answer(outcome)
```

Zero hits or scores below the weak-evidence floor short-circuit to
`insufficient` **without paying an LLM call** — abstention is cheap and the
generator is never asked to compensate for missing evidence (CH03_04's
"thin generation").

## Deviations from the blog's reference stack (recorded on purpose)

| CH03 assumes | We use | Why the swap is safe |
| --- | --- | --- |
| MinerU parsing | our `ocr_backend` (PaddleOCR-VL) + human-proofread sidecar | the contract already normalizes it; the human gate is *stronger* than an automatic trust gate |
| Elasticsearch | SQLite FTS5 + `fold_cjk` | CJK tokenizer gap is already solved and version-pinned here; playground scale |
| Qdrant | none yet — `embeddings` table + protocol slot | sqlite-vec's 2026 maintenance churn says: don't marry an extension before the corpus proves vectors are needed |
| BGE reranker | none yet — post-fusion slot in the engine | rerank earns its keep after hybrid; ordering M2=M3 would be guesswork |
| Instructor/Guardrails | `llm_client.chat_json` + explicit checks | repo rule: one LLM door; CH03_04's own advice that grounding enforcement stays in application logic |
| ACL enforcement | reserved columns only (`documents` has none; chunks inherit doc scope) | single-user repo; an ACL layer with no second user is the "wrapper that forwards parameters" the note set itself warns about |

Deferred with explicit seams instead of implementation: bilingual alignment
(`block_keys`/`paired` fields cheap to add), model-based validation, the
whole CH01/CH02 orchestration path.

## Evaluation is part of the contract

`rag eval golden.jsonl` — cases pin `expected_substrings`, resolved to chunk
ids at query time (stable against rebuilds thanks to content addressing):

```json
{"case_id": "leave-01", "question": "年假超过几天需要审批？",
 "expected_substrings": ["年假", "审批"], "rationale": "表格行级精确查找"}
```

Reported: recall@k, precision@k, hit-rate@k, abstention behaviour on
no-answer cases. First-version thresholds inherit CH04's defaults
(recall ≥ 80%, precision ≥ 70%) *as goals, not as claims* — the numbers
below thresholds are the roadmap: which query classes failed tells us which
protocol seam to fill next (vocabulary gap → DensePath; identifier misses →
shaping; wrong context size → ChunkStrategy).

## Milestones

- **M1 (this package)**: vertical slice — build/query/eval/status CLI,
  lexical path, enrichment optional behind `--enrich`.
- **M2**: `EmbeddingProvider` + `rag embed-backfill` + `DensePath` + real
  RRF — only after M1's eval shows what lexical misses.
- **M3**: rerank slot + query shaping upgrades.
- **M4**: ground `rag query` behind a CH01-style intention gate when the
  orchestration capability is built.

## Deferred decisions (from incremental design)

The incremental design (`05-incremental-design.md`) resolved four open
questions; the decisions affect this document's future milestones:

- **Eval waits for batch completion.** `rag eval` runs after ingest+publish
  finish, not over partial state — partial eval adds noise without value.
- **Publish trigger is deferred.** Daily publish can be cron-scheduled or
  event-driven (monitoring inbox for new data); not a current priority.
- **Inferred hydration is config-controlled.** Which retrieval paths are
  enabled and how many resources they consume is a config + hardware
  question, not a design-time decision.
- **Snapshot retention uses full manifests.** Keep N full snapshots; delta
  chain is deferred until ops data shows the need (years of headroom at
  current scale).

## Run it

Moved to `03-usage.md` (the full CLI reference). Tests:
`uv run pytest tests/test_rag` — model-free (LLM paths are
monkeypatched); the LLM is only reached by explicit `--enrich` / answering.
