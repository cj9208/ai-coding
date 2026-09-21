# RAG Subsystem — Overview

**One sentence:** `src/rag` turns OCR bundles into a versioned, citable
knowledge base — a deterministic offline pipeline publishes snapshots, a
lexical-only online pipeline answers with evidence, and `rag eval` is the
referee for every future technology decision.

```text
why designed this way        what actually shipped        how to run it
docs/rag/01-*.md  ◄───────── docs/rag/02-implementation ─► docs/rag/03-usage
(design rationale)             (module map, constants,      (full CLI reference,
                                deviations from 01)          golden set, traces)

                          where it breaks at 100k PDFs
                          docs/rag/04-scaling.md
                          (250k-chunk benchmark: P50=190ms P95=546ms,
                           FTS5 BM25 = 99% of query time)
                                     │
                                     ▼
                          the redesign, shipped
                          docs/rag/05-incremental-design.md
                          (manifest + fingerprint schema, diff publish,
                           ingest/publish/retract/gc — implemented)

                          trust at 100k docs
                          docs/rag/06-scalable-validation.md
                          (four-layer validation: 10 deterministic checks
                           [shipped], perplexity scoring, sampled QC,
                           feedback loop — Phase 1 implemented)
```

## The document set

| File | Answers | Read it when |
| --- | --- | --- |
| `01-design-rationale.md` | Why contracts are the spine, why plugins with one implementation, why lexical-only is a legitimate M1, deviations from the blog's reference stack | You want to change the architecture and need the load-bearing arguments |
| `02-implementation.md` | The 20 modules of `src/rag` and what each really does, every tuned constant, where the shipped code diverges from the design text | You are reading or modifying `src/rag` |
| `03-usage.md` | Every `rag` subcommand and flag, golden-file format, trace records, exit codes, library entry points | You want to build a corpus, ask questions, or measure |
| `04-scaling.md` | What breaks first at 100k PDFs / 1k per day, why publish must become a manifest swap, what to measure before doing surgery | You are about to grow the corpus past playground scale |
| `05-incremental-design.md` | The v2 schema (manifest + fingerprint), the bounded publish transaction, ingest/publish/retract/gc CLI, migration and test plan | You are implementing (or reviewing) the incremental redesign |
| `06-scalable-validation.md` | Four-layer validation architecture (deterministic checks, perplexity, sampled QC, feedback loop), Phase 1 implementation details | You are extending the trust model or adding new quality checks |

Start with `01` if you have never seen the design; start with `03` if you
just want to run it; `02` is the bridge — it also records the deviations, so
an agent trusting `01` alone will not be surprised.

## Status (2026-09-22)

- **M1 shipped and live-verified**: build / query / eval / status / traces
  CLI, lexical FTS5 path, opt-in `--trace` run records.
- **Incremental pipeline shipped (2026-09-22)**: schema v2 (manifest +
  fingerprint), diff publish, separate `ingest` / `publish` / `retract` /
  `gc` CLI commands. `build` is now a thin compose of `ingest` + `publish`
  — incremental is the only path, full refresh is its degenerate case
  (pipeline fingerprint change ⇒ all ids change ⇒ same ingest loop
  produces a corpus-wide diff).
- **Enrich is an independent background step**: `rag enrich` scans
  unenriched child chunks, calls the LLM concurrently via `TaskQueue`
  (default 4 workers), persists results to the `inferred` table per-chunk
  (checkpoint-per-chunk — a crash loses at most one LLM call). No longer
  a `--enrich` flag on `build`.
- **Validation expanded to 10 checks (2026-09-22)**: the original 3
  (text density, reading-order, table structure) plus 7 new deterministic
  checks targeting VLM failure modes (page-number breaks, char-distribution
  anomaly, n-gram entropy collapse, bbox overlap, table cell inconsistency,
  formula parseability, empty blocks). All zero-cost.
- **Scaling benchmarked (2026-09-22, `04-scaling.md`)**: 250k chunks —
  P50=190ms, P95=546ms, FTS5 BM25 is 99% of query latency, hit_rate=1.0
  at all tiers.
- **M2 (embeddings) is deliberately gated**: only after `rag eval` on a real
  corpus exposes lexical misses. The plug-in points it will use are already
  documented in `02-implementation.md` §"Where the next plug-in lands".
- The corpus itself is not chosen yet — which is *why* the design looks the
  way it does (see `01`).
- **Scalable validation designed (2026-09-21, `06-scalable-validation.md`)**:
  four-layer trust architecture. Phase 1 shipped — 10 deterministic checks.
  Phases 2-4 (perplexity scoring, sampled QC, feedback loop) are design-only.

## Data at a glance

Everything lives under `data/rag/` (REPO_ROOT-anchored; `--data-dir`
overrides):

| Path | What |
| --- | --- |
| `kb.db` | one SQLite file: documents, chunks, snapshot_docs, inferred, chunks_fts, embeddings (empty slot), snapshots, meta |
| `corpus/<doc_id>.md` | markdown projection of every published document — grep-able fallback for a future CandidatePath |
| `traces/<stamp>_<run>_<trace_id>.jsonl` | one file per `--trace` run, rendered with `rag traces` |
