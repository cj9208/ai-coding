# Orchestrator Subsystem — Scaling Investigation

Status: **investigation from code reading (2026-09-22); §1/§2/§5a/§6a/§6b
are now fixed code, and §2/§6a are measured** — the load probe in
§"Verification debt" was written and run the same day and 05a landed
its full executable scope (steps 1–5) the same day; **the "Problem
status ledger" below is the single place to see, per problem, whether it
is measured, fixed, or still open and who owns it**. Every *unmarked*
number below remains an estimate derived from the shipped code
(file:line cited). Unlike `../rag/04-scaling.md` — whose arithmetic was
validated by a 50k-doc run — this document started honest about being
pre-empirical; the bench has now closed the storage gap. It examines
three rungs: the 2k-employee pilot (§1–§6), a 200k-employee
multinational, and a 10M-citizen external service (§"The scenario
ladder") — the hard rungs break *assumptions*, not just estimates.

The scale axis is different, too. RAG's wall is **corpus volume** (100k
PDFs break publish). The orchestrator's wall is **request concurrency +
deployment shape**: per-request data is tens of KB, so SQLite capacity is
a non-issue for years; what breaks is that a harness designed as the
enterprise decision hub has exactly one runtime form — a single-user,
serial, process-per-question CLI — and nowhere in the chain is there an
authentication, tenancy, or quota assumption.

The scenario examined:

| Quantity | Value | Derived |
| --- | --- | --- |
| Employees | 2,000 | — |
| Daily actives | 15% ≈ 300 requests/day | peak ~5 QPS on one gateway |
| LLM calls per request | 2 typical (flash interpret + rag answer), up to 4+ worst case (escalation + retry + switch) | the cost wall arrives before the perf wall |
| Writes per turn | ~15–25 transactions (create + one envelope rewrite per transition + one txn per object + one per event) | `store.py` opens its own transaction per method |
| Stored bytes per request | ~5–8 objects + ~10–20 events ≈ 20 KB | ~2 MB/day, ~150k requests/year, single DB file |
| Clarification gaps | human minutes–hours, accounted out of wall clock (DP-4) | pending requests never expire |

```text
                      where each stage breaks at enterprise scale
 ┌──────────────────────────────────────────────────────────────────────┐
 │ front-half LLM      control loop          store          ops surface  │
 │ 2–4 calls/request ─► sync serial while ──► ~20 txn/turn ─► handoff:   │
 │ no per-user quota    loop, asyncio.run     no busy_timeout  packet+   │
 │      ▲                inside every call    envelope rewrite  export   │
 │      │                                     per transition   only      │
 │ COST WALL      cannot embed in ASGI   lost update on       no TTL on   │
 │                (process-per-turn      concurrent resume    clarif.;    │
 │                                         (no version col)   no tenant   │
 │                                                             filter     │
 └──────────────────────────────────────────────────────────────────────┘
```

## Problem status ledger (2026-09-22, after 05a steps 1–5)

One pass over every problem this document raised, in the order raised,
each with its current state and owner. The data: 05a's baseline and
after benches (`05a-data-plane.md`), landed code, and what is still
only prose here.

| Problem | Status | Owner / note |
| --- | --- | --- |
| §1 deployment shape | **fixed (embeddability) — the host is still open** — `interpret`/`Capability.run` are coroutines, `run_turn_async`/`resume_async` await inside a running loop (pinned by `test_async_surface.py`), sync shims keep CLI/golden/bench unchanged | 05a step 5; what queues behind it is the *service host itself* (gateway, queue worker) — 05b lands identity on it, B.4/B.6 live there |
| §2 write path | **fixed & measured** — explicit `busy_timeout`, per-pass batching (~20→8 commits/turn), `requests.version` CAS, seq on envelope; throughput 45→137 turns/s at 16 writers | 05a steps 2–3; residual: `resume()` still doesn't verify `user_id` — 05b (lost-update hazard already closed by CAS, so this is attribution not integrity) |
| §3 cost/quota | **open, priority raised** — step 4's verdict names LLM quota (with §1) as the *actual* binding constraint once writes are batched | 05c |
| §4 tenancy/identity | **open** — tenant column still absent, `handoff list` still returns every user's packets | 05b |
| §5a clarification never expires | **fixed** — 7-day TTL, resume-past-TTL forces clean re-interpret, `clarification_expired` event keeps it auditable | 05a step 2 |
| §5b handoff black hole | **open** — no worklist, no recovery edge (DP-3 conversation) | 05d |
| §6a unindexed cross-request reads | **fixed & measured** — two indexes; `list_requests(20)` P50 188 ms → <1 ms at 100k rows | 05a steps 2, 4 |
| §6b stale `RagStore` handle | **fixed** — handle reopens when the kb file's `(mtime_ns, size)` changes; in-process publishes stay visible through the cached handle | 05a step 5 (E); 05c's answer cache now has its version key to build on |
| §6c no circuit breaker (`health: degraded`) | **open — now owned**: adopted by 05d (design sketch 5, step 5) after this ledger found it in no sub-plan | 05d, triggered by a capability with real external deps behind the service host |
| §6d `handoff export` linear payload scan | **open, deliberately minor** — ≤200 already-filtered rows, bench-irrelevant | ride-along fix when `cli.py` is next touched |
| A.1 Chinese-only safety gate | **open** | 05b step 1 (independent lane, can start today) |
| A.2 registry governance | **open** — trigger (≈20 entries) not reached; first DP-5 renegotiation pressure | 05d |
| A.3 erasure law vs append-only | **open decision** — must land before the first EU tenant | 05d |
| A.4 config-as-release | **open** | 05d |
| A.5 batching → Postgres, in that order | **batching done; Postgres closed as "won't trigger"** for pilot-A throughput, with named reopen conditions | 05a steps 3–4 (verdict) |
| A.6 handoff = headcount | **open** | 05d |
| B.1 adversarial input | **open** — posture holds structurally, hardening absent | 05b, before any public exposure |
| B.2 LLM call as economic decision | **open** — fast path + answer cache unbuilt | 05c |
| B.3 no idempotency | **open** — one envelope field + one uniqueness check, unbuilt | 05b, before public traffic |
| B.4 streaming latency | **open** — needs a host to own the response stream (embeddability itself is no longer the blocker) | after the service host exists |
| B.5 regional cells | **open by design** — DP-8 intact; nothing to build until a second region exists | A.4 supplies the release mechanics |
| B.6 queue-worker shape | **open** — loop already fits and is now awaitable; host does not exist yet | unblocked by 05a step 5; building it is host work |

Two readings the ledger forces:

- **§6c was the one orphan — the ledger caught it, and 05d adopted
  it.** Every other open problem already had a named sub-plan step;
  the circuit breaker appeared only in this document's derived list
  (item 6, "capability health"). It now has a design sketch, a step
  (05d §5 / step 5), and a trigger (the first capability with real
  external dependencies behind the service host). This is the
  ledger's proof of value: ownership gaps surface only when every
  problem is walked in order.
- **With 05a complete, the open list is one gate and three rooms.**
  The gate is the service host itself — embeddability landed (step 5),
  but nothing hosts the harness yet, and §3/§4/§6c/B.3/B.4/B.6 all
  read "after the host" on their owner line. Behind it sit the trust
  (05b), cost (05c) and governance (05d) rooms, whose triggers are
  calendar/legal, not technical. The storage engine is in no room:
  measured, batched, and closed as "won't trigger" for Postgres.

## What survives the scale-up (keep designing around these)

- **The decision tables and the safety table**: pure Python predicate row
  scans, <20 rows each, microseconds. Ten thousand QPS would not notice.
  Row-count growth (more capabilities ⇒ more routing nuance) is a
  *correctness review* problem, not a perf one — DP-5/DP-7 are contract
  and stay fixed.
- **The seven runtime objects as append-only JSON**
  (`runtime_objects.payload_json`, `extra="ignore"` on `_Contract`,
  `contracts.py:22`): schema evolution is additive, replay/audit is
  structurally guaranteed. Nothing about scale asks this to change.
- **DP-8 (all durable state on the envelope)**: there is *no shared
  mutable runtime state between requests*. Selected capability, counters,
  budgets, wait timestamps — all on one row keyed by `request_id`. This
  makes horizontal scaling embarrassingly easy later: shard by
  `request_id`, and the only shared resources left are the DB file and
  the LLM quota.
- **SQLite capacity itself**: at ~20 KB/request the DB stays under a few
  GB for years. No Postgres migration is on this list; the write
  *discipline* is the problem (§2), not the engine.
- **Cross-process resume** (verified in the M3 live proof, 25 s human
  gap): the checkpoint format already survives process death, which is
  what any queue-based worker design would build on.

## What breaks, ranked by severity

### 1. Deployment shape: no serving layer, and the current code cannot be embedded in one

> **Update 2026-09-22 (05a step 5):** the embeddability half below is
> fixed — the two `asyncio.run` sites became `await`s and the harness
> now runs inside a live event loop (pinned by
> `tests/test_orchestrator/test_async_surface.py`). What remains open
> is the second half of this section's sentence: the serving layer
> itself still does not exist.

The core contradiction. `_drive` is a blocking while-loop
(`runtime.py:179`); both probabilistic stages call `asyncio.run` *inside*
the turn (`interpret.py:117`, `capabilities/rag.py:56`) — under a FastAPI
worker that raises `RuntimeError: asyncio.run() cannot be called from a
running event loop`. So the harness cannot be imported into an ASGI
service as-is.

The CLI-per-turn alternative fails on a different axis: every
`orchestrate ask` re-parses `capabilities.yaml`, re-imports the rag /
file_manager stacks, reopens two more SQLite files — process startup is
the latency floor, and `awaiting_clarification` depends on a human
copy-pasting a request_id.

**This is the decision everything else queues behind**: either make the
runtime awaitable (`await front_half.interpret(...)`, `await cap.run(...)`
— the control-loop *structure* is untouched; capability protocol gets a
coroutine signature), or declare the multi-process CLI the product and
fix §2/§3 around it. The first is the only road into an enterprise
gateway. Until it is decided, the rest of this document is a shopping
list with a blocked checkout.

### 2. The SQLite write path is single-writer by accident, not by design

One `run_turn` performs roughly 15–25 separate transactions:
`create_request`, one **full `envelope_json` rewrite per transition**
(`runtime.py:666` → `store.py:88`, 8–12 per turn), one
`SELECT MAX(seq)+1` + `INSERT` per appended object (`store.py:131`), and
one transaction per event (`store.py:188`). Each `Store` method opens and
commits its own `engine.begin()`.

Three consequences at concurrency > 1 process:

- the shared layer sets only WAL + foreign_keys
  (`src/storage/sqlite.py:53`); there is **no `busy_timeout` and no
  retry**, so the first write collision surfaces as
  `database is locked`;
- `update_request` is an unversioned whole-row overwrite — two processes
  resuming the same `awaiting_clarification` request **silently lose one
  side's counters and state**; and `resume()` does not check `user_id`,
  so nothing even marks who resumed;
- `append_object`'s `MAX(seq)+1` read-then-insert races the same way
  (the `UNIQUE(request_id, kind, seq)` constraint catches same-kind
  collisions but the seq is computed across kinds).

The fixes are local and do not involve changing storage: batch one loop
pass (transition + objects + events) into a single transaction; add a
`version` column to `requests` with compare-and-set; move seq to the
envelope (it is already per-request state).

*All three landed and measured 2026-09-22 (05a steps 2–4), plus an
explicit `busy_timeout` in the shared layer; the only residual from
this section is `resume()` attribution, deferred to 05b with identity.
The `before → after` bench table is in the 05a ledger.*

### 3. Cost and quota: per-request budgets exist, per-user/quota budgets do not

`ExecutionBudget` counts machine *steps* (loops, retries, wall clock) —
nothing counts tokens or money. The worst legal path spends 4+ LLM calls
per request (`MAX_MODEL_ESCALATIONS=1` + `MAX_EXECUTION_RETRIES=2` +
one rag answer generation), and a script around `orchestrate ask` can
loop forever at one user's cost. At 300×(2–4) flash+answer calls/day the
bill, not the latency, is the first thing a finance owner escalates.

Shape of the fix, consistent with DP-9: a `quota_context` on the
envelope (per-user daily LLM-call or token allowance) consumed by the
routing table as a **boolean state** (`quota_exhausted → handoff/reject
row`), not a weighted score — and the escalation counts as consumed
quota, so fallback walks stay legal without new branches.

### 4. Multi-tenancy and identity: the fields exist, the semantics do not

- `ContextRefs.tenant_id = "default"` (`contracts.py:156`) is never
  read; `requests` has no tenant column; `objects_of_kind`
  (`store.py:166`) returns every user's handoffs to whoever runs
  `handoff list`.
- `user_id` is self-asserted (`cli.py:55`, `--user`), and `events`
  stores raw user text (`request_captured` payload carries the full
  input, `runtime.py:156`) — cross-user `replay` visibility is
  unsolved and unbounded.

Unlike §2/§3 this is *not* a late-stage local patch: a tenant column
added after 150k rows means backfilling history that was never written
with tenant intent. If the process model (§1) lands on "service",
identity must arrive with it (gateway asserts user; `resume` verifies
attribution), and tenant filtering goes into every read path the first
time it exists.

### 5. No lifecycle on the two open-ended states

- **Handoff** is DP-3-frozen as packet + markdown export, which was the
  right v1 call — but at 10–50 handoffs/day it becomes an unclaimed
  black hole: no queue state (open/claimed/resolved), no assignee, and
  the deliberate absence of the `handoff → routing` edge
  (`runtime.py:63`) means the human's resolution never returns to the
  state machine. A handoff *worklist table* is additive (it is a
  consumer of persisted packets, not a change to them); the recovery
  edge is a contract conversation, not code.
- **`awaiting_clarification` never expires**: DP-4 correctly accounts
  human wait out of the wall clock, and the side effect is a request
  from 6 months ago is resumable — against a registry, alias table and
  corpus that may have moved on. Needs a TTL (or "on resume after N
  days, force re-interpret and refresh the registry view") — one field
  on the envelope plus one check in `resume()`. *(Landed 05a step 2:
  7-day TTL, no new envelope field was needed — `wait_started_ms` was
  already persisted; a `clarification_expired` event keeps the restart
  auditable.)*

### 6. Capability layer: inherited rag wall + no circuit breaker + stale handles

- The rag adapter is an **in-process function call**, so rag's measured
  P99 (850 ms at 250k chunks, `../rag/04-scaling.md` Benchmark
  results) plus answer generation eat the 30 s wall-clock budget's
  headroom at corpus scale; orchestrator availability is rag
  availability.
- `RagQueryCapability._store` opens `RagStore` once and caches it
  (`capabilities/rag.py:68`): in a long-lived process it **never sees a
  newly published corpus_version**. Harmless in CLI form (process
  lifetime ≈ one question); a standing bug the moment §1 chooses
  "service". *(Fixed 05a step 5E: the handle reopens when the kb
  file's `(mtime_ns, size)` identity changes; 05c's answer cache can
  now key on the resolved store's `active_version`.)*
- A dead downstream has no trip state: every request rediscovers it via
  full timeout, then walks `switch_capability` per-request. The registry
  needs a `health: degraded` boolean the routing/validation tables can
  read — again DP-9-shaped, table-driven, no new control logic.
- Minor but real: `objects_of_kind` filters `kind` and sorts
  `created_at_ms` with **no index** (`store.py:166`, full scan + JSON
  parse per row past ~10^5 objects); `handoff export` linearly scans 200
  payloads to find one id (`cli.py:189`); `list_requests` sorts
  `updated_at_ms` unindexed (`store.py:111`). Two `CREATE INDEX` lines
  fix all three; they are listed here only because scan-per-call is the
  kind of thing that reads fine until it doesn't.

## The scenario ladder: two more aggressive cases (2026-09-22)

The pilot scenario above (2,000 employees) is the scale the code was
*verified* at. Two harder rungs answer a different question: not "which
estimate gets bigger" but "which architectural assumption stops being
true". Same posture as the rest of this document — code-read, no
benchmark; every claim cites the file that makes it.

| | pilot: 2k employees | **A: multinational, 200k employees** | **B: external service, city of 10M** |
| --- | --- | --- | --- |
| requests/day | 300 | 100–200k (50% DAU, 1–2/employee) | 100–200k normal; 1–2M event day (outage, policy deadline) |
| peak QPS | <1 | 50–100 sustained | 500–1,000 burst |
| LLM calls/day (2–4/request) | ~1k | 300–600k — provider rate limits become a capacity topic | 300k–8M — a seven-figure annual line item |
| store writes/day (~20 txn/turn, §2) | ~6k | 2–6M | 20–50M |
| bytes/day (~20 KB/request) | ~6 MB | 2–4 GB (~1 TB/year) | 2–40 GB (7–15 TB/year) |
| handoffs/day | ~10 | 2–4k at a 2% rate → **40–80 human FTE** | ≤0.5% or the call center drowns (500–5k/day) |
| who the caller is | self (`--user`) | SSO-asserted, per-org | **anonymous public** |

### Case A — what stops being true (not just what gets bigger)

1. **The safety gate is Chinese-only and silently language-bypassable.**
   All six rows of `safety.py:50-103` match Chinese patterns; a French
   or English "wipe the database" request matches nothing and lands on
   `s0_allow` — the hard-stop layer is *absent* for every other language,
   and nobody gets an error. The fix is not translation: DP-2's assets
   (safety table, `normalize.ALIASES`, the Chinese `SYSTEM_PROMPT` in
   `interpret.py:74`) must become **per-locale packs**, and
   `OriginalInput.locale` — carried today, consumed by nothing (verified:
   zero reads) — finally becomes a routing input. This is the first
   place where "Chinese is first-class" quietly becomes "Chinese only".
2. **The registry is one team's file.** At 50–200 capabilities,
   `select()`'s first-match-in-declaration-order (DP-5 single-level) is a
   collision hazard, and four catalog fields that exist *for* governance
   — `rollout_status`, `cost_profile`, `latency_profile`,
   `use_when`/`avoid_when` — are write-only: no runtime decision reads
   them (verified). At this rung they must either feed selection or be
   deleted. **This is the first genuine renegotiation pressure on DP-5**
   — the correct move is a contract edit with a reason, not a silent
   code drift.
3. **Append-only objects collide with erasure law.** GDPR/PIPL "right to
   be forgotten" requires deleting user text, which lives in
   `envelope_json`, every `runtime_objects.payload_json`, and `events`
   payloads. The replay guarantee (`01`) and erasure are both desirable;
   the architecture currently picks neither. Candidate resolutions
   (crypto-erase via per-user/tenant keys; tombstone projections that
   preserve row-ids but blank text) are **product decisions the code
   does not make today** — and they must be made before the first EU
   tenant, not after the first complaint.
4. **Tenancy hardens from column to topology.** Data-residency regimes
   mean per-region databases anyway; that makes §4's tenant column easy,
   but it promotes the problem: **configuration is also tenant data**.
   `capabilities.yaml`, thresholds, alias tables, budgets become
   per-region artifacts that must be versioned and released (the golden
   suite is exactly the release gate for them), not files an operator
   edits under a running service.
5. **The DB question is answered, and the ordering is §2-first.**
   2–6M transactions/day ends SQLite; `src/storage` already anticipates
   the `postgres.py` sibling adapter. But batching the per-turn writes
   (pilot §2) *must* land first: 20 round-trips per turn × 100 QPS is a
   connection-pool story, not an engine story, and changing the engine
   first just moves the bottleneck into PG.
6. **Handoff is headcount, not a log line.** DP-3's packet+export is an
   artifact *format*; A needs it wired to a real ticket system (assign,
   SLA, resolution callback), and the validation table's
   `partial_answer` economics becomes a staffing lever, since at
   10–50 agent-handled tickets/FTE/day the handoff *rate* is an opex
   number the tables directly control.

### Case B — everything in A, plus the trust model inverting

1. **Input is adversarial.** The pilot assumed an internal user with
   nothing to gain; B assumes attackers. A regex gate is bypassable by
   homoglyphs, encoding, language switch (see A.1), fragmentation.
   Injection targets the *proposal* side: `ModelInterpretation` already
   absorbs model junk (`_coerce_bool`, `interpret.py:57`), proving the
   seam holds structurally — but a jailbroken `confidence=1.0` can still
   walk route r7. Posture holds; what's missing is untrusted-input
   hardening *outside* the harness: upstream moderation, authN,
   device/IP rate limiting before `run_turn`, and adversarial golden
   cases as a test class.
2. **The per-turn LLM call is an economic decision, and the escape hatch
   is already built.** `interpret.py` calls the model on every
   non-refused turn. `normalize` already computes exactly the evidence
   that would justify skipping flash interpretation for high-frequency
   intents (alias hit with `top_match_score ≥ STRONG_TOP_MATCH`,
   `candidate_count ≤ 1` → `strong_evidence`, `assess.py:144`). At B's
   cost curve, `DeterministicSignals` graduates from confidence input
   (DP-9) to a **cost gate**: a deterministic fast path that bypasses
   the front-half model, plus `(normalized_query, corpus_version)`
   answer caching — the first lever in `../rag/04-scaling.md`'s
   query-side options lands here for money, not latency.
3. **No idempotency.** `run_turn` mints `request_id` server-side
   (`contracts.py:202`); an external retry storm creates N billable,
   stateful requests. A client-supplied idempotency key is one field on
   the envelope and one uniqueness check in `create_request` — cheap,
   and it must exist before public traffic, not after the first
   incident-day double-bill.
4. **Latency expectations invert the budget's meaning.** 30 s is fine
   for batch thinking and terrible for citizen chat; first-token
   expectations (~2 s) force streaming on the answer stage, which
   changes who owns the response text (capability streams, `FinalOutcome`
   persists the final render). Contract-neutral, implementation-heavy.
5. **Multi-region is the easy part — DP-8 pays off exactly here.**
   Event-day traffic + residency → regional cells. Because *all* durable
   state is one per-request row (pilot "What survives"), active-active
   per region needs no shared runtime state, no distributed transaction —
   just per-cell Postgres and released config artifacts (A.4). The
   harness's oldest design decision is the one that scales furthest.
6. **The queue-worker shape is now forced, and the loop already fits
   it.** Pilot §1's fork resolves: async service, workers pulling turns
   from a queue. `run_turn`/`resume` are already
   resume-across-process-boundaries by construction (the M3 live proof
   was exactly this); the control loop does not change — its host does.

### What the ladder strengthens (do not touch these)

- **DP-7 row-id audit becomes a compliance product, not an engineering
  nicety.** In B, a citizen complaint can be answered with "these exact
  table rows fired, in this order" — `orchestrate replay` is the
  regulatory defense. This is the strongest argument the deterministic
  harness is the right core at *both* rungs.
- **Budget-boundedness is what keeps worst-case cost finite** at 1,000
  QPS of hostile retries: a request structurally cannot loop. A.2/B.2
  reduce average cost; DP-4 caps the tail.
- **Per-request state (DP-8)** — the only pilot-era decision that gets
  *more* valuable at every rung.

### Revisions to the derived-work list

The pilot §1 decision ("process model") is **taken out of the
hand at both A and B — it is 'async service'**. What the ladder adds,
ordered:

1. (A, before any non-Chinese tenant) **per-locale safety/alias/prompt
   packs**, with `locale` consumed at the front half;
2. (A, before the first EU tenant) **erasure strategy** — crypto-erase
   vs tombstone, decided and documented against the replay guarantee;
3. (A+B, first capacity work) **write batching → Postgres sibling
   adapter** (in that order);
4. (B, before public exposure) **idempotency key + authN + rate limit +
   adversarial golden class**;
5. (B economics) **deterministic fast path + answer cache**, sized by a
   real cost model (one afternoon: calls × tokens × price at B volume);
6. (A, when entries > ~20) **registry governance**: selection consumes
   `rollout_status`/`cost_profile`/`latency_profile`, or they die —
   a contract (DP-5) conversation, tracked like DP-3's handoff question;
7. (A+B) **config-as-release**: capabilities/thresholds/alias packs
   versioned, gated by the golden suite, shipped per region.

Still *not* on the list at any rung: changing the seven runtime objects,
the tables' first-match-row-id semantics, or DP-8. They are the parts
the ladder keeps proving right.

## The redesign hinge: the process model is the only real decision

Everything above sorts into two buckets, and the split is worth stating
plainly because it is the opposite of rag's situation:

```text
 rag:  one schema hinge (version = manifest, not copy) collapsed §1–§3
       because the DATA model was wrong at scale.

 here: the data model is right (per-request state, append-only objects,
       DP-8 — nothing needs distributing). What is wrong is the
       PROCESS model: sync/serial, per-call event loops, per-statement
       transactions, self-asserted identity.
```

So the derived work is ordered by exactly one dependency: §1 decides
where code runs; §2–§5 are local, and only §2 must be redone if §1
picks "multi-process CLI" instead of "async service" (transaction
batching differs; optimistic locking is needed by both).

## Verification debt (the benchmark to run before any surgery)

**Measured 2026-09-22** — the probe was written (`scripts/orch_bench_run.py`,
zero-LLM via the golden machinery) and run against current code; all
three numbers live in `05a-data-plane.md`'s baseline section. Headline:
~20 txns/turn confirmed; zero lock errors but P99 32 ms → 2.2 s from
1 → 8 writers (pysqlite's 5 s busy-timeout hides contention as tail
latency), 0.39% hard failures at 16; `list_requests(20)` at 100k
requests: P50 188 ms. What was proposed:

- `scripts/orch_bench_run.py`: **stub the LLM and capabilities** (the
  FakeFrontHalf + ScriptedCapability already exist — reuse the golden
  machinery), N worker processes hammering one `orchestrator.db` with
  legal state-machine walks.
- Measure three things:
  1. write contention: `database is locked` rate and P99 per turn vs.
     worker count (turns §2's estimates into a curve);
  2. per-turn transaction count × duration (the batching win, before
     and after);
  3. `status` / `handoff list` latency after 10^5 requests seeded with
     scripted traffic (turns §6's index claim into numbers).
- A real-LLM cost probe (calls/day × token profile) is a spreadsheet,
  not a benchmark — worth one afternoon when §3 gets designed.

## Derived work, in dependency order

State as of 2026-09-22 in the "Problem status ledger" above; this list
keeps the original dependency order with markers.

1. **Decide the process model** (§1): ✅ *decided and coded* — "async
   service" at both ladder rungs; 05a step 5 made the harness
   awaitable inside a running loop. The host that runs it (gateway,
   queue worker) is not built — that is the remaining half of §1.
2. ✅ **Write discipline** (§2) — shipped in 05a steps 2–3 and
   bench-verified in step 4: one transaction per loop pass;
   `requests.version` compare-and-set; seq onto the envelope.
   Residual: `resume()` attribution checking deferred to 05b.
3. **Identity + tenancy land together** (§4): gateway-asserted user_id,
   tenant column on `requests` + filter in every read path — open, 05b.
4. **`quota_context` as a boolean routing signal** (§3): budget field +
   one routing row + golden cases for the exhausted path — open, 05c.
5. ✅ **Cheap storage wins** (§6) — shipped: two indexes (bench-
   verified), clarification TTL. Still open from this item:
   per-corpus-version store handles (05a step 5E).
6. **Capability health + handoff worklist** (§5/§6): additive tables and
   one degraded-signal row; the handoff→routing recovery edge is a
   contract (DP-3) conversation first. Open, 05d — both halves now
   carried (worklist step 2, health step 5; the health row addition is
   itself a DP-5 table change and flagged as such).

Deliberately *not* on this list: any storage-engine migration (SQLite is
years from capacity trouble here), any change to the four decision
tables' shape or the DP-5 single-level selection, any distributed state
between turns (DP-8 means there is none to distribute).
