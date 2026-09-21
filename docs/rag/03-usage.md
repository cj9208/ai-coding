# RAG Subsystem — Usage Guide

The complete `rag` CLI reference. For what the stages guarantee read
`02-implementation.md`; for why they exist read `01-design-rationale.md`.

## Prerequisites

- **OCR bundles** — `*.ocr.json` files under an inbox dir, produced by
  `ocr-backend parse <file> --out <dir>` (the default inbox is
  `data/ocr_backend/out`). A `*.review.json` sidecar beside a bundle is
  folded in automatically and upgrades the document's trust.
- **LLM env** — only `rag query` (answering) and `rag enrich` touch the
  model; they read `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL` from the
  repo-root `.env` (shared `llm_client` convention). `--retrieve-only` and
  plain builds need nothing.
- Install: the `rag` entry point ships with the editable project install;
  prefix everything with `uv run`.

## Command reference

```text
rag [--data-dir PATH] COMMAND ...

  build   [--inbox DIR] [--trace]                offline: ingest + publish
  ingest  [--inbox DIR] [--limit N] [--trace]    stage new/changed docs from inbox
  publish                                        run the diff transaction (staged -> active)
  enrich  [--limit N] [--prompt-ver V] [--workers N]  LLM-annotate unenriched chunks
  retract <doc_id>                               mark a doc for removal at next publish
  gc      [--keep N]                             prune old snapshots and orphaned chunks
  status                                         snapshot / document overview
  query   QUESTION [-k N] [--retrieve-only] [--trace]   retrieval (+ answer)
  eval    GOLDEN.jsonl [-k N] [--trace]          metrics on a golden set
  traces  [FILE] [-n N]                          list / render run traces
```

`--data-dir` defaults to `data/rag/` at the repo root; the SQLite file,
`corpus/` projection and `traces/` all live under it.

### build

```bash
uv run rag build --inbox data/ocr_backend/out
```

A thin compose of `ingest` + `publish` — kept for tests and toy corpora.
For production use, run `ingest` and `publish` separately (see below).
Deterministic; prints a JSON report (`bundles`, `decisions`, `doc_count`,
`chunk_count`, `errors`, `corpus_version`, `staged_added`,
`retracted_removed`); a bundle that fails to parse lands in `errors` and
the rest still builds (exit 1 flags that).

### ingest

```bash
uv run rag ingest --inbox data/ocr_backend/out
uv run rag ingest --limit 10          # stop after 10 bundles
```

Scans the inbox, skips bundles already staged/live with the current
pipeline fingerprint (idempotent), chunks new/changed docs, and stages
them. Each run is deterministic; prints a JSON report (`bundles`,
`decisions`, `chunked_docs`, `skipped_docs`, `errors`). A broken bundle
skips with an error line, it never kills the ingest.

### publish

```bash
uv run rag publish
```

Runs the diff transaction: copies the active manifest, adds staged docs,
removes retracted docs, diffs FTS, inserts the snapshot row, flips
`meta.active_version`. Prints a JSON report (`corpus_version`,
`doc_count`, `chunk_count`, `staged_added`, `retracted_removed`). Readers
never see a half-built index.

### enrich

```bash
uv run rag enrich
uv run rag enrich --limit 100         # stop after 100 chunks
uv run rag enrich --workers 8         # 8 concurrent LLM calls
uv run rag enrich --prompt-ver enrich_v2   # use a different prompt version
```

Independent background step: scans live child chunks lacking an `inferred`
row for the current prompt version, calls the LLM concurrently via
`TaskQueue` (default 4 workers), and persists results to the `inferred`
table (checkpoint-per-chunk — a crash loses at most one LLM call). After
enrichment, refreshes the affected FTS rows so the new keywords/summary
enter the index.

Prints a JSON report (`prompt_ver`, `pending`, `enriched`, `errors`,
`docs_refreshed`). Without `rag enrich` the pipeline stays fully
deterministic; chunks are searchable via the FTS fallback (section_path
title, empty keywords / summary).

### retract

```bash
uv run rag retract d_abc123def456
```

Marks a document for removal at the next `publish`. The doc's
`ingest_state` flips to `retracted`; the next publish removes it from the
manifest and deletes its FTS rows. The chunks themselves survive until
`gc` prunes them.

### gc

```bash
uv run rag gc
uv run rag gc --keep 5         # retain the 5 most recent snapshots
```

Garbage-collects old snapshots and orphaned chunks. Retains the N most
recent snapshots (default 3), deletes `snapshot_docs` rows older than
that, deletes chunks not referenced by any retained snapshot, deletes
`inferred` rows not referencing any live chunk, and deletes old
`snapshots` rows. Prints a JSON report (`chunks_deleted`,
`inferred_deleted`, `snapshots_pruned`).

### status

```bash
uv run rag status
```

JSON: documents grouped by publish decision, chunk counts per staged/live
version, the active version, and the snapshot list. First stop when a query
behaves strangely — a build that errored mid-way leaves the *previous*
snapshot active, and this is how you see that.

### query

```bash
uv run rag query "年假超过几天需要审批？" -k 5          # retrieval + answer
uv run rag query "年假超过几天需要审批？" --retrieve-only  # evidence only, no LLM
```

Output order: the evidence pack (candidate count, best FTS score, each
`[n] chunk_id (type) section :: first line`), then `outcome:` plus the
answer text and one line per claim with its `[refs]`. `clarify` prints its
question; `notes:` reports citation damage. Exit 0 for `answered`/
`partial`, **2** for `clarify`/`insufficient`/`escalated` — scriptable.

`--retrieve-only` is the debugging workhorse: it shows what retrieval
believed *before* blaming the generator. Zero hits or a best score under
the weak-evidence floor short-circuits to `insufficient` without any LLM
call, so a cheap `--retrieve-only` run tells you whether the offline side or
the online side is at fault.

### eval

```bash
uv run rag eval tests/golden/rag_sample.jsonl
uv run rag eval tests/golden/rag_sample.jsonl -k 8
```

Golden file is JSONL, one case per line:

```json
{"case_id": "leave-01", "question": "年假超过几天需要审批？",
 "expected_substrings": ["年假", "审批"], "rationale": "表格行级精确查找"}
```

- `expected_substrings` resolve to chunk ids **at eval time** against the
  active snapshot (a chunk matches when its text contains every
  substring) — rebuild-proof and readable; never pin chunk ids by hand.
- Empty `expected_substrings` = abstention test: passes when the pack comes
  back insufficient (also proves the no-LLM short-circuit fired).
- A case whose substrings match nothing is reported under
  `unresolved_case_ids` and exits 1 — fix the case before trusting the
  metrics.

Summary JSON: `hit_rate`, `recall`, `precision` (means over retrieval
cases), `abstention_correct`, `per_case` rows. Goals from CH04:
recall ≥ 80%, precision ≥ 70% — as targets, not claims; the failed query
*classes* are the roadmap (see 01 §Evaluation).

### traces

Any of build/query/eval accepts `--trace`: the run is recorded as one
JSONL file of OTel-shaped spans under `<data-dir>/traces/`, and the path is
printed as `trace: ...` at the end of the normal output.

```bash
uv run rag query "..." --trace        # → data/rag/traces/20260921-..._query_t-....jsonl
uv run rag traces                     # list the 10 most recent runs
uv run rag traces 20260921-124949     # substring match → render the span tree
uv run rag traces -n 30               # list more
```

Rendering shows per-span timing and summary attributes, e.g.:

```text
trace t-20260921-124949-91e2 — 5 span(s), 812.3 ms
rag.query  812.3 ms  question=年假超过几天需要审批？
  rag.retrieve  41.2 ms  n_candidates=5  insufficient=False
    rag.shape  0.1 ms  n_tokens=4
    rag.path.fts.search  12.4 ms  hits=10  relaxed=False
  gen_ai.completion  748.9 ms  outcome=answered  n_claims=2
```

Use it to answer "where did the time go" (LLM vs FTS vs publish) and "why
did it abstain" (relaxed flag, n_candidates) without rerunning anything.

## Library use

The CLI is a thin shell; the composition roots are importable:

```python
from rag.pipeline import build, ingest, publish
from rag.engine import retrieve
from rag.answer import generate

# Full pipeline (for tests / toy corpora)
report = build(inbox, data_dir)              # dict, same shape as CLI JSON

# Production use: separate ingest and publish
ingest_report = ingest(inbox, data_dir)      # stage new/changed docs
publish_report = publish(data_dir)           # run the diff transaction

pack = retrieve(store, "问题", k=5)          # EvidencePack; insufficient flag honored
answer = asyncio.run(generate(pack, "问题"))  # Answer(outcome, claims[...])
```

`retrieve(store, ...)` needs a live `RagStore(data_dir / "kb.db")`. Passing
`shaped=` to `retrieve` lets an experiment swap the shaper without touching
the engine.

## Tests

```bash
uv run pytest tests/test_rag          # model-free end to end
```

No real LLM call happens in tests (monkeypatched at `llm_client`). The
end-to-end loop worth running by hand once, on a fresh clone:
`ocr-backend parse` a PDF → `rag build` → `rag query --retrieve-only` →
`rag query` → `rag eval` a golden file built from what you just found.
