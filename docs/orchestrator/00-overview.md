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
| `04-scaling.md` | What breaks first at 300 requests/day (process model, write discipline, quota, tenancy) — and which assumptions stop being true at 200k employees (multinational) or a 10M-citizen external service (scenario ladder) | You are about to take the runtime beyond single-user CLI scale |
| `05a-data-plane.md` | The first sub-plan: bench harness before any surgery, async embeddability, batched + compare-and-set writes, a bench-gated Postgres phase | You are starting scale work — this unblocks everything else |
| `05b-trust-boundary.md` | How the four trust holes close: per-locale packs (start now), identity + tenancy on the service host, idempotency + adversarial goldens before public exposure | You touch safety, identity, or any external-facing surface |
| `05c-cost-gate.md` | How per-request LLM spend gets cut: boolean quota gate, deterministic fast path with fired-row parity, corpus-versioned answer cache | Money, quota, or the B-rung cost curve is the topic |
| `05d-lifecycle-governance.md` | The three scheduled decisions (erasure, registry fields, config-as-release) and the additive handoff worklist | You own the governance calendar or the handoff queue |

Start with `01` if the harness idea is new to you; `03` if you just want to
ask it questions; the contract first if you intend to *change* behavior;
`05a`–`05d` (via `04`) if the task is the scaling work itself.

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
- **Scale work started 2026-09-22 and 05a is complete.** `04-scaling.md`
  plus four sub-plans (`05a` data plane → `05b` trust boundary →
  `05c` cost gate → `05d` lifecycle/governance). All five executable 05a
  steps landed the same day: bench harness (`scripts/orch_bench_run.py`)
  with before/after sweeps; write batching + version CAS + TTL +
  indexes + busy_timeout (throughput 45→137 turns/s at 16 workers;
  `list_requests` 188 ms→<1 ms at 100k rows); and the async surface —
  `interpret`/`Capability.run` are coroutines, `run_turn_async`/
  `resume_async` embed the harness in a running event loop while sync
  shims keep CLI/golden/bench unchanged (repo suite 551 green).
  Postgres is closed as "won't trigger"; the *service host* is the
  next build, and 05b–05d await their triggers. 04 carries a
  **problem status ledger** — read it first for per-problem state.

## Data at a glance

Everything lives under `data/orchestrator/` (REPO_ROOT-anchored; `--db`
overrides); `data/` is gitignored:

| Path | What |
| --- | --- |
| `orchestrator.db` | one SQLite file, three tables: `requests` (the envelope JSON + status column), `runtime_objects` (append-only typed objects, per-request seq), `events` (the replay feed `orchestrate replay` reads) |
| `config/orchestrator/capabilities.yaml` | the production registry view — one entry per capability, 11 required fields enforced at load |
| `config/orchestrator/golden_cases.jsonl` | 22 scripted regression cases feeding the fake front half — zero LLM, zero corpus |
