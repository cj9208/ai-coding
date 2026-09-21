# RAG Subsystem — Implementation Guide

What `src/rag` actually is today: 19 modules, ~2400 lines, M1 vertical
slice. This document walks the shipped code — responsibilities, constants,
and the points where reality diverged from `01-design-rationale.md`. When
the two disagree, **this document is right about the code, and 01 is right
about the intent**.

## Module map

```text
                        ┌─ contracts that never change shape ─┐
 contract.py   180 L    six data objects (CanonicalDoc, Chunk, Candidate,
                        EvidencePack, Answer, Outcome) + id functions
 protocols.py    67 L    five Protocol seams, one implementation each
                        └──────────────────────────────────────┘

 OFFLINE (build)                     ONLINE (query)
 ingest.py      63   Acquirer        shape.py     48   QueryShaper (CJK split)
 structure.py   69   section paths   fts_path.py  76   the only CandidatePath
 validate.py    93   publish gate    fuse.py      38   RRF, single-path today
 chunking.py   203   parent/child    assemble.py 104   pack + strength + floor
 enrich.py      58   opt-in LLM      answer.py   138   thin gen + citation gate

 COMPOSITION ROOTS AND PLUMBING
 pipeline.py    85   build() orchestrator        engine.py    72   retrieve()
 store.py      373   RagStore, 6 tables, publish cli.py     224   5 subcommands
 tracing.py   332   Tracer + Traced* shells      evaluation.py 127 golden harness
```

Imports are one-directional: `cli` → `pipeline`/`engine`/`evaluation` →
stage modules → `contract`/`protocols`. Stage modules never import the CLI
and never see the tracer — tracing is attached only at the roots.

## The offline run: `rag build`, step by step

`pipeline.build(inbox, data_dir, enrich=False, tracer=...)` — for each
`*.ocr.json` in the inbox (sorted, so runs are reproducible):

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
3. **Validate** (`validate.assess`) — deterministic proxy metrics only:

   | Check | Flag | Severity |
   | --- | --- | --- |
   | < 30 chars/page | `low_text_density` | high |
   | < 50% blocks ordered | `reading_order_sparse` | medium |
   | table without `<table`/`<tr`/`\|` | `table_unstructured` (one per bad table) | high |
   | no source sha256 | `missing_source_hash` | instant `fail` |
   | zero text at all | `no_text` | instant `fail` |

   Two high flags — or one high plus sparse ordering — quarantine; any flag
   warns; clean passes. **A reviewed document upgrades out of quarantine to
   `pass_with_warning`**: the human gate beats any proxy metric (the design
   rule that makes this legal lives in 01 §Storage; the code is
   `validate._decide`).
4. **Upsert & gate** — every document (including quarantined ones) goes to
   the `documents` table, inspectable; only `pass`/`pass_with_warning` get
   chunked (`pipeline.INDEXABLE`).
5. **Chunk** (`chunking.StructureAwareChunker.split`) — group blocks by
   section path, then within a section: tables and formulas are atomic
   one-block chunks, adjacent list items merge into one `list` chunk,
   prose merges until `CHILD_CHAR_LIMIT = 800` chars. A section with >1
   child also gets a `section` **parent** chunk (children carry
   `parent_chunk_id`). Chunk text is a `\n\n` join of block contents —
   markdown as a *rendering*, block_keys retained for citation.
6. **Enrich (opt-in)** — `LlmEnricher` fills `chunk.inferred`
   (title/keywords/summary) for children only, capped at `max_chunks=500`,
   input truncated at 2000 chars, temperature 0. Without `--enrich` the
   whole build touches no LLM.
7. **Publish** (`store.stage_chunks` + `store.publish`) — stage under
   `version = max(largest version, active) + 1` (old snapshots survive for
   rollback), then **one transaction**: wipe `chunks_fts`, re-index this
   version's chunks, insert the `snapshots` row with
   `REPRESENTATIONS = {"fts": "ready", "vector": "absent@bge-m3"}`, flip
   `meta.active_version`. Readers never see a half-built index.
8. **Project** — `data/rag/corpus/<doc_id>.md` written from the published
   documents (`ocr_backend.render.document_markdown`), the grep-able
   insurance policy.

The CLI prints the JSON report (bundles, decisions, chunk/doc counts,
errors) and exits 1 if any bundle errored — a broken bundle skips with an
error line, it never kills the build.

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

Six tables, not the five 01 sketches (`store._DDL`):

| Table | Notes beyond 01's sketch |
| --- | --- |
| `documents` | gained `page_count`, `created_at` columns for status output |
| `chunks` | PK is `(chunk_id, corpus_version)`, **not** `chunk_id` — content-addressed ids repeat across versions *by design*; versioned history is what rollback reads |
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
| `Acquirer.fetch(bundle_paths) -> list[CanonicalDoc]` | `fetch(bundle: Path) -> CanonicalDoc`, the loop lives in `pipeline.build` | per-bundle granularity is what lets one broken bundle skip instead of poisoning the batch |
| "five tables" | six: `meta` added | the active-version pointer needs a home; a snapshot row is not a pointer |
| `chunks(chunk_id PK)` | PK `(chunk_id, corpus_version)` | 01 §3 already demands surviving old snapshots; the composite key is that requirement made real |
| "fuse (RRF; identity today)" | genuine RRF + dedupe already | pinning the fusion seam with tests now is cheaper than discovering it bends in M2 (01's own argument, accepted) |
| `EvidencePack`: "ordered chunks" | chunks **plus a separate non-citable `parents` list** | parent expansion is CH03_02's idea; citability had to be explicit or every citation would double-count |
| shaper "can expand keywords via llm_client" | only the deterministic split; no LLM shaper exists | that was a listed option, not an M1 promise |
| no abstention number | `_WEAK_EVIDENCE_FLOOR = 1e-3` | calibrated on this repo's corpora (see assemble.py comment); it is an input to future eval, not a law |
| CLI in 01 lists build/query/eval | five subcommands: + `status`, `traces`, plus `--trace` | status = the operator's first question; traces grew from the run-record need, both documented in 03 |

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

## Tests

`tests/test_rag/` mirrors the stages; all model-free (LLM monkeypatched):

| File | Pins down |
| --- | --- |
| `test_ingest.py`, `test_structure_validate.py` | bundle acquisition, sidecar folding, section paths, gate decisions |
| `test_chunking.py` | atomicity, window split, parent wiring, content-addressed ids |
| `test_store_publish.py` | staging, atomic publish, rollback survival, representations |
| `test_online.py` | shaper CJK rule, relaxation, floor → insufficient, pack assembly |
| `test_answer.py` | short-circuit, ref validation, downgrade matrix |
| `test_evaluation_cli.py` | golden parsing, unresolved loudness, exit codes |
| `test_tracing.py` | span tree, error re-raise, failure degrades to warning |
