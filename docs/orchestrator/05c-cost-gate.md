# Orchestrator Scaling Sub-plan 05c — Cost Gate

Status: **proposed 2026-09-22** — derived from `04-scaling.md`; one of
four sub-plans (05a–05d). Suggested execution order: **3rd** — its
quota step can go earlier; the rest waits for a cost model and 05a's
service.

**One sentence:** cut per-request LLM spend with a boolean quota gate,
a deterministic fast path that bypasses flash interpretation, and a
corpus-versioned answer cache — each sized by a real cost model before
it is built.

Parent breaks: `04-scaling.md` §3 (per-user quota missing), B.2
(per-turn LLM call as an economic decision).

## Why this angle third

Money is the wall that arrives before latency at every rung: the worst
legal path spends 4+ LLM calls per request (`MAX_MODEL_ESCALATIONS=1`
+ `MAX_EXECUTION_RETRIES=2` + answer generation), and a script around
`orchestrate ask` can loop forever at one user's cost. But the two big
levers (fast path, cache) only pay off in a long-lived, high-volume
process — 05a's service — and only make sense once a cost model says
how much they save. The quota gate is the exception: it is
envelope + table + golden work with no dependency, and it defends the
pilot against script-loop abuse today.

## Trigger

- Cost model (step 1): one afternoon; it gates everything after
  step 2.
- Quota gate (step 2): now — cheap and DP-9-shaped.
- Fast path + cache (steps 3–4): when the cost model shows per-turn
  flash interpretation is a real line item at the target volume (it
  does at rung B: 300k–8M calls/day), and after 05a step 5.

## Design sketch

### 1. Cost model first

A spreadsheet, not a benchmark (`04-scaling.md`'s own words): calls
per request by outcome path × token profile × price, at pilot / A / B
volumes; the same table with fast-path + cache hit rates applied.
Output: the thresholds that decide whether steps 3–4 are worth
building, and what hit rate the cache needs to pay for itself. Until
this exists, any "optimization" is unpriced.

### 2. `quota_context` as a boolean routing signal (§3)

DP-9 shape, not a weighted score:

- envelope field: per-user (per-tenant) daily allowance of **LLM
  calls**, consumed at the call sites — flash interpret, model
  escalation, and answer generation each count once;
- one new routing row: `quota_exhausted → handoff/reject`;
- escalation counts as consumed quota, so fallback walks stay legal
  without new branches;
- a `QuotaGate` wrapper sits at the LLM boundary (front half +
  capabilities), keeping the counting in one place.

**This adds a row to the routing table — the contract
(`docs/orchestrator-design.md`) owns the tables.** The change lands as
a contract amendment (row, predicate, golden cases), the same
discipline the parent demands for DP-3/DP-5 questions.

### 3. Deterministic fast path (B.2)

`normalize` already computes the evidence that justifies skipping flash
interpretation: an alias hit with `top_match ≥ STRONG_TOP_MATCH` and
`candidate_count ≤ 1` — exactly `strong_evidence`
(`assess.py:144`). Design:

- when `strong_evidence` holds, the task type is in a small
  fast-path-eligible set, safety raised no flags, and quota is
  available → **skip the front-half model call** and synthesize the
  `ModelInterpretation` deterministically from the normalize output;
- the synthesized interpretation is the *same contract object* the LLM
  path produces — downstream tables cannot tell the difference. The
  fast path is just another proposer with zero tokens; the harness
  decides identically (DP-9's "propose, never decide" extended
  leftward);
- behind a config flag, default off until goldens prove parity.

The parity golden is the angle's centerpiece: **the same input through
the flash path and the fast path must emit identical fired row-ids.**
If they diverge, the fast path is wrong, not the tables.

### 4. Answer cache

- Key: `(tenant_id, normalized_query, capability, corpus_version,
  locale)` — the whole determinism surface. `corpus_version` in the
  key is what makes the cache correct across rag republishes; it only
  works because 05a step 5 fixed the stale `RagStore` handle
  (`capabilities/rag.py:68` cached forever) — that fix is this cache's
  prerequisite and lands there.
- Value: the validated `FinalOutcome` (plus citations) — cache only
  *answered* outcomes with `grounding_coverage ≥` the configured
  minimum; rejected/clarified paths never cache.
- Store-backed (new table), not in-memory: survives restarts, is
  tenant-scoped by construction, and keeps DP-8 (nothing shared and
  mutable between requests).
- Read-through at the capability boundary *after* validation, so a
  cache hit still records its own objects/events — replay never lies
  about which rows fired.

## Step sequence

1. Cost model one-pager → go/no-go thresholds for steps 3–4.
2. Quota context + routing row + `QuotaGate` + goldens for the
   exhausted path (independent of 05a; can land while 05a is
   mid-flight).
3. Fast path behind flag + parity goldens (needs step 1's numbers to
   justify; needs nothing else).
4. Answer cache table + read-through + corpus_version keying (needs
   05a step 5).

## Verification

- Cost model table reproduced in this doc's status header once
  written.
- Quota: exhausted-path golden; escalation-consumes-quota golden.
- Fast path: parity suite (same fired row-ids on both paths) across
  the existing golden inputs where eligible.
- Cache: a hit returns the identical outcome object; a republish bump
  (corpus_version changes) → miss; cross-tenant key isolation test.
- Suite green; the flag defaults off.

## Non-goals

- No weighted scoring anywhere (DP-9): quota is boolean, fast-path
  eligibility is boolean, cache eligibility is a coverage threshold.
- No token-precision accounting — day-grain call counts first.
- No streaming of partial answers (B.4 stays unplanned).
- The fast path never bypasses **safety** — the gate runs before any
  path, deterministic or not (DP-2).
