# RAG Subsystem — Implementation Guide

What `src/rag` actually is today: 20 modules, ~2800 lines, M1 vertical
slice with incremental pipeline. This document walks the shipped code —
responsibilities, constants, and the points where reality diverged from
`01-design-rationale.md`. When the two disagree, **this document is right
about the code, and 01 is right about the intent**.

## Module map

```text
                        ┌─ contracts that never change shape ─┐
 contract.py   180 L    six data objects (CanonicalDoc, Chunk, Candidate,
                        EvidencePack, Answer, Outcome) + id functions
 protocols.py    67 L    five Protocol seams, one implementation each
 versions.py     32 L    pipeline fingerprint + prompt version
                        └──────────────────────────────────────┘

 OFFLINE (ingest + publish)        ONLINE (query)
 ingest.py      63   Acquirer      shape.py     48   QueryShaper (CJK split)
 structure.py   69   section paths fts_path.py  78   the only CandidatePath
 validate.py   292   10 checks     fuse.py      38   RRF, single-path today
 chunking.py   227   parent/child  assemble.py 104   pack + strength + floor
 enrich.py     252   concurrent    answer.py   138   thin gen + citation gate
                    LLM via TaskQueue

 COMPOSITION ROOTS AND PLUMBING
 pipeline.py   146   ingest()/publish()/build()  engine.py    72   retrieve()
 store.py      658   RagStore, 8 tables, diff    cli.py     332   10 subcommands
                     publish
 tracing.py    332   Tracer + Traced* shells      evaluation.py 127 golden harness
```

Imports are one-directional: `cli` → `pipeline`/`engine`/`evaluation` →
stage modules → `contract`/`protocols`. Stage modules never import the CLI
and never see the tracer — tracing is attached only at the roots.

## The offline run: `rag build` (= `rag ingest` + `rag publish`)

`pipeline.build(inbox, data_dir, tracer=...)` composes `ingest()` +
`publish()`. For production use, run them separately — `ingest` stages
new/changed docs, `publish` runs the diff transaction.

### Ingest: `pipeline.ingest(inbox, data_dir, tracer=...)`

For each `*.ocr.json` in the inbox (sorted, so runs are reproducible):

1. **Acquire** (`ingest.OcrBundleAcquirer.fetch`) — parse the bundle into
   `OcrDocument`; if a `<name>.review.json` sidecar sits beside it, fold it
   with `ocr_review.patch.apply_review` and mark `source.reviewed=True`
   (the machine JSON on disk is never mutated). `doc_id` =
   `"d" + sha256(source.sha256)[:12]` — re-acquiring the same file is an
   upsert, never a duplicate.
2. **Structure** (`structure.rebuild_section_paths`) — title blocks form a
   heading stack; the level comes from a numeric prefix (`2.1 ` → 2), else
   1; titles truncate at 60 chars. Every non-furniture block inherits the
   current `" > "`-joined path. Furniture = header/footer/page_number blocks,
   excluded entirely; unordered blocks sort after ordered ones per page.
3. **Validate** (`validate.assess`) — 10 deterministic proxy metrics:

   | Check | Flag | Severity |
   | --- | --- | --- |
   | < 30 chars/page | `low_text_density` | high |
   | < 50% blocks ordered | `reading_order_sparse` | medium |
   | table without `<table`/`<tr`/`\|` | `table_unstructured` (one per bad table) | high |
   | page numbers not non-decreasing | `page_number_non_monotonic` | high |
   | bbox overlap (>30% of smaller block) | `bbox_overlap` (one per overlap) | high |
   | symbol ratio >15% or zero CJK+Latin | `char_distribution_anomaly` | medium |
   | 4-gram entropy <2.0 bits | `low_ngram_entropy` | medium |
   | markdown table column count varies | `table_cell_inconsistent` (one per bad table) | medium |
   | LaTeX braces unbalanced or empty | `formula_unparseable` (one per bad formula) | medium |
   | >10% empty-content blocks | `empty_blocks` | medium |
   | no source sha256 | `missing_source_hash` | instant `fail` |
   | zero text at all | `no_text` | instant `fail` |

   Two high flags — or one high plus sparse ordering — quarantine; any flag
   warns; clean passes. **A reviewed document upgrades out of quarantine to
   `pass_with_warning`**: the human gate beats any proxy metric (the design
   rule that makes this legal lives in 01 §Storage; the code is
   `validate._decide`).
4. **Upsert & gate** — every document (including quarantined ones) goes to
   the `documents` table with `ingest_state='staged'`, inspectable; only
   `pass`/`pass_with_warning` get chunked (`pipeline.INDEXABLE`).
5. **Skip if already staged/live** — `_already_staged()` checks
   `(ingest_state, chunked_with_fp)`; if the doc is already staged or live
   with the current pipeline fingerprint, skip it (idempotent ingest).
6. **Chunk** (`chunking.StructureAwareChunker.split`) — group blocks by
   section path, then within a section: tables and formulas are atomic
   one-block chunks, adjacent list items merge into one `list` chunk,
   prose merges until `CHILD_CHAR_LIMIT = 800` chars. A section with >1
   child also gets a `section` **parent** chunk (children carry
   `parent_chunk_id`). Chunk text is a `\n\n` join of block contents —
   markdown as a *rendering*, block_keys retained for citation. Chunk IDs
   are content-addressed: `sha256(pipeline_fp | doc_id | section_path |
   block_keys)[:12]` — a settings change produces new IDs naturally.
7. **Stage** (`store.stage_document`) — INSERT chunks (idempotent on
   `chunk_id`), flip doc to `ingest_state='staged'`, record
   `chunked_with_fp` and `chunk_count`.
8. **Project** — `data/rag/corpus/<doc_id>.md` written from the indexed
   documents (`ocr_backend.render.document_markdown`), the grep-able
   insurance policy.

### Publish: `store.publish_diff(representations)`

The diff transaction (see `05-incremental-design.md` for the full design):

1. Copy the active manifest (`snapshot_docs` rows for `active_version`) to
   a new `corpus_version`.
2. Add staged docs (those with `ingest_state='staged'` and matching
   `chunked_with_fp`) to the new manifest.
3. Remove retracted docs (those with `ingest_state='retracted'`).
4. Diff FTS: upsert rows for newly listed `(doc_id, fp)` pairs, delete rows
   for removed pairs. FTS title = inferred title if enriched, else last
   segment of `section_path`.
5. Insert the `snapshots` row with `REPRESENTATIONS = {"fts": "ready",
   "vector": "absent@bge-m3"}`.
6. Flip `meta.active_version` to the new version.
7. Flip staged docs to `ingest_state='live'`.

Readers never see a half-built index. A full rebuild is the degenerate case
(pipeline_fp change ⇒ every chunk ID changes ⇒ the diff covers everything).

The CLI prints the JSON report (bundles, decisions, chunk/doc counts,
errors, corpus_version, staged_added, retracted_removed) and exits 1 if any
bundle errored — a broken bundle skips with an error line, it never kills
the build.

## Enrich: `rag enrich` (independent background step)

Enrich is **not** part of `build`. It runs as a separate command, scanning
live child chunks that lack an `inferred` row for the current prompt
version, calling the LLM, and persisting results to the `inferred` table.

`LlmEnricher` uses `TaskQueue` with a dedicated "enrich" lane for
concurrent LLM calls (default 4 workers). Each chunk is committed
individually (checkpoint-per-chunk) — a crash loses at most one LLM call,
and a re-run picks up where it left off. Backpressure propagates naturally:
when the lane queue is full, `submit()` blocks; when LLM rate limiting
kicks in, workers block, the queue fills, and `submit()` blocks.

The enricher fills `title`, `keywords`, `summary` for children only
(parents are skipped — their text is the union of children). Input
truncates at 2000 chars, temperature 0.0. After all chunks are enriched,
`refresh_fts_for_docs()` re-upserts the affected FTS rows so the new
keywords/summary enter the index.

The `inferred` table is keyed by `(chunk_id, prompt_ver)` — a prompt
change triggers re-enrichment of affected chunks without touching the
pipeline fingerprint (enrich is a rebuildable projection, not a
boundary-affecting setting). `PROMPT_VER = "enrich_v1"` lives in
`versions.py` alongside the pipeline fingerprint.

Without `rag enrich` the pipeline stays fully deterministic; chunks are
searchable via the FTS fallback (section_path title, empty keywords /
summary).

## The online run: `rag query`, step by step

`engine.retrieve(store, question, k=5)`:

1. **Gate on the snapshot** — no `active_version`, or a snapshot whose
   `representations` register no live path, returns an insufficient
   `EvidencePack` immediately. A projection that exists but was never
   backfilled is *invisible*, not half-effective: `build_paths` only
   instantiates paths whose representation string starts with `ready`.
2. **Shape** (`shape.LexicalShaper`) — the CJK rule is the interesting
   part: this machine's FTS5 `fold_cjk` expands CJK runs to chars+bigrams,
   so one long run as a single token would demand an exact contiguous
   phrase. Runs of ≤3 chars stay one token; longer runs are cut into 2-char
   segments (`match_expr` ANDs the folded groups). ASCII words lowercase.
3. **Search** (`fts_path.Fts5Path`) — `bm25(chunks_fts, 5.0, 1.0, 2.0,
   1.5, 1.0)` positional over (title, body, section_path, keywords,
   summary); children only (`is_parent = 0`); requests `k*2` candidates.
   Graded relaxation: strict AND first, retry OR on zero hits, recorded as
   `meta["relaxed"]`. Scores flip sign once at the edge (`-bm25`) so every
   consumer sees higher = better.
4. **Fuse** (`fuse.rrf_fuse`) — real RRF (`1/(60 + rank)`) even with one
   path; fused score/rank stored under the `"rrf"` key.
5. **Assemble** (`assemble.assemble`) — take top-k, load chunks, compute
   `strength = {n_candidates, best_score, paths}`. Two insufficiency
   branches, both before any LLM call: zero candidates, or best score below
   `_WEAK_EVIDENCE_FLOOR = 1e-3` (a real hit on this repo's corpora scored
   three orders of magnitude above a stray token surviving OR relaxation).
   Then parent expansion: each hit's section parent rides along as
   background context, capped at `_MAX_PACK_CHUNKS = 12` — parents are
   **not citable** (their text is the union of children).

## Answering: thin generation, then pure code

`answer.generate(pack, question)` short-circuits to `insufficient` if the
pack is insufficient — abstention never pays for a token. Otherwise one
`llm_client.chat_json` call (temperature 0.2) with numbered evidence
(`[n] (doc_id p3-4; type; section: …)`) plus unnumbered `[context-j]`
parents; chunk text truncates at 1500 chars (parents at 2×). The generator
picks one of the five outcomes; `validate_answer` is then deterministic:

- unknown/invalid outcome string → `insufficient`;
- claim refs outside `1..n` are stripped, claims left with no refs are
  counted and dropped from the checked list;
- `answered` downgrades to `partial` if any evidence chunk is missing, any
  claim lost refs, or any claim was uncited.

CLI exit codes: 0 for `answered`/`partial` (and always for
`--retrieve-only`), **2** for `clarify`/`insufficient`/`escalated`.

## Storage: what the schema ended up being

Eight tables, not the five 01 sketches (`store._DDL`):

| Table | Notes beyond 01's sketch |
| --- | --- |
| `documents` | gained `page_count`, `created_at`, `ingest_state`, `chunked_with_fp`, `chunk_count` columns; `ingest_state` tracks the staged/live/retracted lifecycle |
| `chunks` | PK is `chunk_id` (content-addressed, includes `pipeline_fp`), **not** `(chunk_id, corpus_version)` — the composite key was replaced by the manifest approach; `pipeline_fp` is now a column, not part of the PK |
| `snapshot_docs` | **new** — the manifest table; `(corpus_version, doc_id, pipeline_fp)` rows list which docs are in which snapshot. This is what makes publish a bounded diff instead of a full rebuild |
| `inferred` | **new** — enrich side projection; `(chunk_id, prompt_ver)` PK, title/keywords/summary. Keyed by prompt version so a prompt change triggers re-enrichment without touching pipeline_fp |
| `chunks_fts` | `storage.FtsTable` over (title, body, section_path, keywords, summary), rowid-joined to `chunks`. Title = inferred title if enriched, else last segment of `section_path` |
| `embeddings` | created, empty, never read in M1 — the M2 join key is the content-addressed `chunk_id`. M2 vector storage decision: sqlite-vec is the probe-first candidate (IVF/DiskANN indexes, pre-v1 risk accepted); `VectorStore` protocol abstraction (`04-scaling` §6) keeps LanceDB/pgvector as exit ramps |
| `snapshots` | as designed; `representations` is the authority on which paths `build_paths` runs |
| `meta` | **new** — single-purpose key/value holding `active_version`; the pointer flip is what makes publish atomic |

Reads for eval: `chunk_ids_matching(version, substrings)` resolves golden
expectations with `text LIKE '%s%'` AND-ed over all substrings, children
only — no id pinning anywhere.

## Evaluation mechanics

`evaluation.evaluate` runs each case through the same `engine.retrieve`:

- case with `expected_substrings` → resolve to chunk ids at query time;
  zero matches ⇒ case is **unresolved** (reported loudly, exit 1 — an
  empty expectation is the harness's own only failure mode);
- case *without* substrings → abstention test: pass iff the pack came back
  insufficient (this also verifies the no-LLM-call short-circuit);
- summary: `hit_rate`, mean `recall`, mean `precision` (denominator is k,
  not returned count), `abstention_correct`, `unresolved_case_ids`,
  `per_case` rows.

Sample file: `tests/golden/rag_sample.jsonl` (two retrieval cases + one
abstention case against a fictional leave-policy doc).

## Tracing: OTel's model, no dependency

`tracing.py` (332 lines, the largest module) exists so a run leaves a
process record **without a single tracing line inside a stage module**. The
mechanism:

- `Tracer.span()` is a context manager; nesting is implicit via a
  `ContextVar` (same trick as OTel's context); span ids are dotted paths
  (`1`, `1.2`, `1.2.1`) so export order reconstructs the tree.
- Span attributes follow GenAI semantic conventions where one fits
  (`gen_ai.operation.name`, `gen_ai.retrieval.top_k`, …). The exporter is
  one JSONL file per run; a real OTel/Phoenix importer could read the same
  records without a call-site change.
- Two guarantees: a business exception is recorded (`status=error`,
  `error.type/message`) and **re-raised unchanged**; a tracing failure
  (disk full, bad dir) degrades to one stderr warning and the run proceeds.
- `NO_TRACER = Tracer.disabled()` — every composition root defaults to it,
  so untraced code paths are branch-free.

The `Traced*` shells wrap exactly the five seams plus generation:

| Wrapper | Span | Summary facts recorded (never payloads) |
| --- | --- | --- |
| `TracedAcquirer` | `rag.acquire` | doc_id, page_count, decision, flag count |
| `TracedChunker` | `rag.chunk` | n_chunks, n_parents |
| `TracedEnricher` | `rag.enrich` | n_chunks (batch span only — per-chunk spans would explode a 500-chunk build) |
| `TracedShaper` | `rag.shape` | n_tokens |
| `TracedPath` | `rag.path.<name>.search` | hits, top-3 chunk ids, relaxed |
| *(cli)* | `gen_ai.completion` | outcome, n_claims |

Root spans: `rag.build` / `rag.query` + `rag.retrieve` /
`rag.eval` + `rag.eval_case`. Read back with `rag traces` (see 03).

## Where the shipped code diverges from 01

Recorded so nobody re-derives the delta:

| 01 says | Code does | Why it's fine |
| --- | --- | --- |
| `Acquirer.fetch(bundle_paths) -> list[CanonicalDoc]` | `fetch(bundle: Path) -> CanonicalDoc`, the loop lives in `pipeline.ingest` | per-bundle granularity is what lets one broken bundle skip instead of poisoning the batch |
| "five tables" | eight: `meta`, `snapshot_docs`, `inferred` added | the manifest approach (`snapshot_docs`) replaces "a version owns rows"; `inferred` is the enrich side projection; `meta` holds the active-version pointer |
| `chunks(chunk_id PK)` | PK `chunk_id` (content-addressed, includes `pipeline_fp`) | the composite key `(chunk_id, corpus_version)` was replaced by the manifest approach — chunks are now immutable and fingerprint-scoped, not version-owned |
| "fuse (RRF; identity today)" | genuine RRF + dedupe already | pinning the fusion seam with tests now is cheaper than discovering it bends in M2 (01's own argument, accepted) |
| `EvidencePack`: "ordered chunks" | chunks **plus a separate non-citable `parents` list** | parent expansion is CH03_02's idea; citability had to be explicit or every citation would double-count |
| shaper "can expand keywords via llm_client" | only the deterministic split; no LLM shaper exists | that was a listed option, not an M1 promise |
| no abstention number | `_WEAK_EVIDENCE_FLOOR = 1e-3` | calibrated on this repo's corpora (see assemble.py comment); it is an input to future eval, not a law |
| CLI in 01 lists build/query/eval | ten subcommands: + `status`, `traces`, `ingest`, `publish`, `enrich`, `retract`, `gc`, plus `--trace` | incremental pipeline split build into ingest/publish; enrich became independent; retract/gc manage the lifecycle |
| enrich "opt-in `--enrich`" | independent `rag enrich` command with concurrent LLM calls via TaskQueue | enrich is a rebuildable projection (keyed by `prompt_ver`), not a boundary-affecting setting — separating it from build keeps the pipeline deterministic |

## Where the next plug-in lands

M2's four-touch path, per the design — the code already has the slots:

1. **`VectorStore` protocol** (`src/rag/vector_store.py`) — the storage
   abstraction that decouples the query engine from the vector backend.
   Two methods: `add(chunk_id, vector)` and `search(vector, k) -> list[chunk_id]`.
   The sqlite-vec implementation is the probe-first choice (`04-scaling` §6);
   LanceDB or pgvector are swap-in replacements behind this protocol.

2. `EmbeddingProvider` writes `embeddings` keyed on existing `chunk_id`
   (a backfill job diffs `content_hash` to skip unchanged chunks);

3. flip `snapshots.representations["vector"]` to `ready@<model>` — nothing
   else knows;

4. one `DensePath` file + one line in `engine.build_paths`. `Candidate`,
   `rrf_fuse`, `assemble`, `answer` do not change — that is the bet, and
   `rag eval` is what calls it.

## Known gotchas (from production audit 2026-09-22)

These bit us during the production readiness audit. When modifying the RAG
module, check for these patterns:

**N+1 queries kill performance.** Looping over items and issuing one DB
query per item (e.g., per-chunk FTS upsert, per-chunk enrich pre-check)
becomes O(N×M) round-trips. Fix: batch with `IN (...)` clauses, window
functions for related data. See `_batch_upsert_fts`, `_batch_load_inferred`,
`_batch_already_enriched`.

**Cache keys must include all inputs that affect output.** The retrieval
cache key was `sha256(question:k)`, missing the shaper output. A shaper
change (different CJK tokenization) would return stale cached results.
Fix: compute shaped query before cache check, include tokens in key.

**SQLite defaults to DEFERRED transactions.** Two concurrent `publish_diff`
calls can read the same `active_version`, compute the same `v_new`, and
collide on insert. Fix: `BEGIN IMMEDIATE` at transaction start acquires a
write lock up front, serializing concurrent publishes.

**`list[Any]` hides type bugs.** `enrich()` accepted `list[Any]` but only
worked with `Chunk` objects. Fix: use `list[Chunk]` so mypy catches
mismatches. Same for enum-over-string: `SourceInfo.kind: str` →
`SourceKind(StrEnum)` catches typos at the contract boundary.

**Dict validators don't check keys by default.** Pydantic's
`dict[str, float]` validates values but not key constraints. Fix: add
`field_validator` for non-empty keys, catching data errors at the boundary
rather than deep in fusion/assembly.

**LIKE wildcards need escaping.** `chunk_ids_matching` substrings
containing `%` or `_` were interpreted as wildcards. Fix: escape `\`, `%`,
`_` and add `ESCAPE '\'` clause.

**Don't swallow partial failures.** Enrich's LLM calls failed silently —
the caller couldn't detect partial failure. Fix: collect errors, raise
`EnrichPartialError(errors)` so the caller can report.

**Disabled code paths should avoid allocation.** The disabled tracer
allocated a new `Span` object on every `span()` call. Fix: pre-allocate a
`_NO_OP_SPAN` singleton, zero allocation when tracing is off.

## Tests

`tests/test_rag/` mirrors the stages; all model-free (LLM monkeypatched):

| File | Pins down |
| --- | --- |
| `test_ingest.py`, `test_structure_validate.py` | bundle acquisition, sidecar folding, section paths, 10 validation checks, gate decisions |
| `test_chunking.py` | atomicity, window split, parent wiring, content-addressed ids |
| `test_store_publish.py` | staging, diff publish, rollback survival, representations, retract, gc |
| `test_online.py` | shaper CJK rule, relaxation, floor → insufficient, pack assembly |
| `test_answer.py` | short-circuit, ref validation, downgrade matrix, enricher persistence |
| `test_evaluation_cli.py` | golden parsing, unresolved loudness, exit codes, CLI build/status/query flow |
| `test_tracing.py` | span tree, error re-raise, failure degrades to warning |
| `test_enrich.py` | write_inferred idempotency, enricher skips already-enriched and parents, concurrent execution with TaskQueue |
