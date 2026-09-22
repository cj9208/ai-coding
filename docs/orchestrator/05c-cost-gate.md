# Orchestrator Scaling Sub-plan 05c — Cost Gate

Status: **executing 2026-09-22** — steps 1 (cost model) and 2 (quota
gate) landed; the routing-row change went through as a contract
amendment (`docs/orchestrator-design.md`, "Amendment 2026-09-22: the
quota row"). Step 3's GO and step 4's NO-GO-for-now are the cost
model's conclusions — see §1.

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

### 1. Cost model first — written 2026-09-22

A spreadsheet, not a benchmark (`04-scaling.md`'s own words). The call
structure is read off the shipped code (caps in `config.py`, counting in
`runtime.py`, `rag.answer.generate`'s skip); token profiles are measured
anchors (assembled zh prompt + injected JSON schema + output) at CJK
≈ 1 char/token; prices are DeepSeek list price as **placeholder until a
deployment's rate card replaces them** — the ratios, not the yuan, carry
the decisions.

LLM calls per request by outcome path:

| path | front-half | capability | total |
| --- | --- | --- | --- |
| safety hard stop / unsupported locale | 0 | 0 | **0** |
| clarify-only (r3/r4) | 1 | 0 | **1** |
| direct answer (r7→e6→v5) | 1 | 1 | **2** |
| escalated answer (r6→r7) | 2 | 1 | **3** |
| retry/switch en route | 1–3 | 2–3 | **3–6** |
| worst legal walk (loops=6, escal=1, retries=2) | ≤3 | ≤3 | **≈6** |

Per-call token profile (typical, zh): flash interpret ≈ 0.8k in + 0.3k
out; answer generate ≈ 2k in (k=5 OCR chunks of 300–800 chars) + 0.5k
out; escalation ≈ 3× flash price. At flash ¥1/¥2 per M and escalated
¥3/¥6:

| unit | ¥ |
| --- | --- |
| one flash interpret | ≈ 0.0014 |
| one answer generate | ≈ 0.003 |
| direct answer request | ≈ 0.0044 |
| escalated answer request | ≈ 0.0087 |

Volumes from `04-scaling.md` §"aggressive rungs", and the annual spend
the two big levers would attack (mix assumption: 70% direct, 15%
escalated, 10% clarify, 5% retry/switch):

| rung | requests/day | monthly spend | front-half (flash) share |
| --- | --- | --- | --- |
| pilot (300) | 300 | ≈ ¥45 — quota is *abuse defense*, saving nothing here | ≈ ¥15 |
| Case A (150k) | 100–200k | ≈ ¥20–27k | ≈ ¥6–9k (¥70–110k/yr) |
| Case B event day (2M) | — | ≈ ¥10k **per day** | ≈ ¥3k |

**Go/no-go the model outputs:**
- **Step 3 (fast path): GO.** At Case A the per-turn flash interpret is
  a ¥70–110k/yr line item, exactly as the trigger predicted for B; at
  pilot it costs nothing to leave a flag-off, parity-tested fast path in
  the repo (its build cost is one-time, the panic-time cost isn't).
- **Step 4 (answer cache): NO-GO for now.** The big lever is answer
  generation (≈ 68% of request spend), but its saving is
  hit-rate × that 68% — and *nobody has ever measured the repeat rate*,
  while the cache only pays inside a long-lived, high-volume process
  that does not exist yet (05a built the data plane, not the host). The
  prerequisite is already met (`corpus_version` keying survives rag
  republishes via 05a step 5's handle fix — see `capabilities/rag.py`
  `_get_store`); what's missing is data. Reopen when the service host
  has run real traffic and its logs can estimate the hit rate.
  Until then, building it is the unpriced optimization this section
  exists to prevent.

### 2. `quota_context` as a boolean routing signal (§3) — landed

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

**Step 2 implementation notes (landed 2026-09-22; deltas from the
sketch above):**

- **There is no `QuotaGate` wrapper class.** The sketch wanted the
  wrapper to keep counting "in one place"; what actually owns that
  place is `runtime.py` — a turn has exactly two points that can spend
  a model call (`_interpret`, `_execute`), and only the runtime touches
  the counter from either side. A wrapper around the front half and one
  around each capability would have re-created the two homes the
  wrapper was meant to avoid. The sketch's one-place property holds by
  construction, and `CapabilityResult.llm_calls` keeps the *reporting*
  with the only party that can know (DP-6 posture: the runtime adds a
  reported number, it never infers one).
- Quota is a **`RoutingSignals` boolean + its own row**, not a
  `CapReached`: caps feed r2 via `any_budget_exhausted`, so a cap-shaped
  quota would fire r2 and the audit row would blame the loop budget.
  Position after r2, before r3 (a quota-spent user must not reach
  clarify or escalation); the id is append-only (`route_r10`) because
  shipped row ids are audit references (DP-7).
- Consumption semantics pinned by goldens g18/g19: the front-half pass
  counts **after** it happened (a refusal that short-circuited with
  `model_name="none"` costs nothing); escalation counts each re-
  interpretation; day-grain means the row can gate up to one call late
  by design.
- The day read is `Store.llm_calls_today(user, now)` — a `json_extract`
  sum over the user's envelopes in the current **UTC** day (v1
  simplicity; per-tenant local days belong with 05b's identity step),
  **excluding the in-flight request** because its own spend lives on
  the live envelope — summing both would double-count (DP-8). Backed by
  a new `(user_id, created_at_ms)` index; old envelopes without the
  counter return NULL and add zero.
- **Found while wiring the goldens:** `RequestEnvelope.new(budget=...)`
  kept the *caller's* `ExecutionBudget` object, so two live envelopes
  from one template would have shared a mutable budget — and the
  runtime mutates it (`wall_clock_paused_ms`). Now `.model_copy()`s it.
  Golden cases additionally run as `golden:<case_id>` users, since the
  suite shares one database and one case's quota spend must not leak
  into the next case's routing.

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

1. ✅ Cost model one-pager → written §1; verdicts: step 3 GO, step 4
   NO-GO pending hit-rate data.
2. ✅ Quota context + routing row + counting + goldens (2026-09-22;
   counting lives in the runtime, not a wrapper — see §2 notes).
3. Fast path behind flag + parity goldens (needs step 1's numbers to
   justify; needs nothing else). — GO per §1
4. Answer cache table + read-through + corpus_version keying (needs
   05a step 5 — landed — and measured hit rate — missing). — parked
   per §1

## Verification

- ✅ Cost model: the §1 tables, with prices marked placeholder until a
  deployment's rate card replaces them.
- ✅ Quota: exhausted-path golden (g18); escalation-consumes-quota
  golden (g19); row-position test (r2 before r10 before r3); day-sum
  store tests (UTC window, exclusion, legacy rows).
- Fast path: parity suite (same fired row-ids on both paths) across
  the existing golden inputs where eligible.
- Cache: (parked) a hit returns the identical outcome object; a
  republish bump (corpus_version changes) → miss; cross-tenant key
  isolation test.
- Suite green; the flag defaults off.

## Non-goals

- No weighted scoring anywhere (DP-9): quota is boolean, fast-path
  eligibility is boolean, cache eligibility is a coverage threshold.
- No token-precision accounting — day-grain call counts first.
- No streaming of partial answers (B.4 stays unplanned).
- The fast path never bypasses **safety** — the gate runs before any
  path, deterministic or not (DP-2).
