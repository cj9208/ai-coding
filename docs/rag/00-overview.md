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
                          (investigation: publish must become
                           an incremental manifest swap, not a
                           full rebuild — nothing measured yet)
                                     │
                                     ▼
                          the redesign, concretely
                          docs/rag/05-incremental-design.md
                          (manifest + fingerprint schema, publish
                           diff, CLI shape, migration, test plan)
```

## The document set

| File | Answers | Read it when |
| --- | --- | --- |
| `01-design-rationale.md` | Why contracts are the spine, why plugins with one implementation, why lexical-only is a legitimate M1, deviations from the blog's reference stack | You want to change the architecture and need the load-bearing arguments |
| `02-implementation.md` | The 19 modules of `src/rag` and what each really does, every tuned constant, where the shipped code diverges from the design text | You are reading or modifying `src/rag` |
| `03-usage.md` | Every `rag` subcommand and flag, golden-file format, trace records, exit codes, library entry points | You want to build a corpus, ask questions, or measure |
| `04-scaling.md` | What breaks first at 100k PDFs / 1k per day, why publish must become a manifest swap, what to measure before doing surgery | You are about to grow the corpus past playground scale |
| `05-incremental-design.md` | The v2 schema (manifest + fingerprint), the bounded publish transaction, ingest/publish/retract/gc CLI, migration and test plan | You are implementing (or reviewing) the incremental redesign |

Start with `01` if you have never seen the design; start with `03` if you
just want to run it; `02` is the bridge — it also records the deviations, so
an agent trusting `01` alone will not be surprised.

## Status (2026-09-21)

- **M1 shipped and live-verified**: build / query / eval / status / traces
  CLI, lexical FTS5 path, opt-in `--enrich`, opt-in `--trace` run records.
- **M2 (embeddings) is deliberately gated**: only after `rag eval` on a real
  corpus exposes lexical misses. The plug-in points it will use are already
  documented in `02-implementation.md` §"Where the next plug-in lands".
- The corpus itself is not chosen yet — which is *why* the design looks the
  way it does (see `01`).
- **Scaling investigated (2026-09-21, `04-scaling.md`)**: at 100k PDFs the
  full-rebuild publish is the core contradiction. Agreed direction:
  version-as-manifest plus a pipeline fingerprint in `chunk_id` —
  incremental is the only path, full refresh is its degenerate case.
  Now specced concretely in `05-incremental-design.md` (design of
  record, pre-implementation). All of 04's numbers remain estimates —
  the 5k-doc measurement run has not happened yet.

## Data at a glance

Everything lives under `data/rag/` (REPO_ROOT-anchored; `--data-dir`
overrides):

| Path | What |
| --- | --- |
| `kb.db` | one SQLite file: documents, chunks (the only truth), chunks_fts, embeddings (empty slot), snapshots, meta |
| `corpus/<doc_id>.md` | markdown projection of every published document — grep-able fallback for a future CandidatePath |
| `traces/<stamp>_<run>_<trace_id>.jsonl` | one file per `--trace` run, rendered with `rag traces` |
