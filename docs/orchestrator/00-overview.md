# Orchestrator — Overview

**One sentence:** `src/orchestrator` is the deterministic harness in which
the LLM only proposes and the code decides — four decision tables, a
budget-bounded state machine and seven typed runtime objects persisted to
SQLite, with RAG and structured lookup as pluggable capabilities behind one
registry.

```text
 which decisions are locked and why
 docs/orchestrator-design.md          ◄── THE CONTRACT (DP-1..DP-10,
 (decisions authority; per-milestone       M0-M3, implementation notes)
  notes; do not re-derive from the
  blog note set)

why shaped this way        what actually shipped        how to run it
docs/orchestrator/01-*   ◄──────── docs/orchestrator/02 ────────► docs/orchestrator/03
(design rationale, the         (module map, control loop,        (full CLI reference,
 harness-vs-model split,        constants, adapter postures,     golden-case format,
 deliberate simplifications)    derived-signal rules)            live-proof recipes)
```

## The document set

| File | Answers | Read it when |
| --- | --- | --- |
| `../orchestrator-design.md` | Which decisions are taken and closed (DP-1..DP-10), the M0–M3 build order and what each milestone fixed in code | Before changing anything — it is the implementation contract and the decisions authority |
| `01-design-rationale.md` | Why the LLM is only allowed behind one seam, why tables are data with row ids, why budgets live on the envelope, what was deliberately simplified vs. the blog's reference architecture | You want to change the architecture and need the load-bearing arguments |
| `02-implementation.md` | The 20 modules of `src/orchestrator` and what each really does, the four-step control loop, every tuned constant, the three M3 signal/semantics rules | You are reading or modifying `src/orchestrator` |
| `03-usage.md` | Every `orchestrate` subcommand and flag, the golden-case file format, how to reproduce both live proofs, the recipe for adding a third capability | You want to run it, test it, or extend the registry |

Start with `01` if the harness idea is new to you; `03` if you just want to
ask it questions; the contract first if you intend to *change* behavior.

## Status (2026-09-22)

- **M0–M3 all shipped and live-verified in one day.** 145 orchestrator
  tests (zero-LLM except the stubbed ones), 22 golden cases, the full repo
  suite green.
- **Two live proofs stand as acceptance records:**
  1. M2 — `ask "春晖省钱卡每月抵扣上限是多少"` → rag answer with two
     anchored citations, `grounding_coverage 1.0`, exec_e6 → val_v5.
  2. M3 — `ask "冷邮件活动的简报 PDF 是谁上传的"` → r3 clarify → resume
     across a 25 s human gap (wall clock unharmed) → rag misses in 8 ms →
     **exec_e4 switches to structured_lookup** → real file_manager record
     accepted. The switch row of the table is not theoretical anymore.
- **Next is not a milestone:** eval-driven hardening — more golden cases
  per table row, confidence thresholds earned by labeled cases, and the
  permission-profile enforcement seam (see contract "Explicitly Not Doing").

## Data at a glance

Everything lives under `data/orchestrator/` (REPO_ROOT-anchored; `--db`
overrides); `data/` is gitignored:

| Path | What |
| --- | --- |
| `orchestrator.db` | one SQLite file, three tables: `requests` (the envelope JSON + status column), `runtime_objects` (append-only typed objects, per-request seq), `events` (the replay feed `orchestrate replay` reads) |
| `config/orchestrator/capabilities.yaml` | the production registry view — one entry per capability, 11 required fields enforced at load |
| `config/orchestrator/golden_cases.jsonl` | 22 scripted regression cases feeding the fake front half — zero LLM, zero corpus |
