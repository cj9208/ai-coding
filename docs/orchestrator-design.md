# Enterprise Orchestrator — Design

**One sentence:** `src/orchestrator` is a deterministic control runtime —
decision tables, a budget-bounded state machine, seven typed runtime objects
persisted to SQLite — in which the LLM only proposes and the harness decides;
RAG is the first capability behind it.

Status: **M0–M3 shipped (all milestones complete; ask -> rag cited answer and
rag -> lookup switch both live-verified), next: eval-driven hardening**
(2026-09-22).
The blog note set
`content/blog/AI_study/rag-orchestration-architecture/` in the
`cj9208.github.io` repo is the conceptual source; this document is the
implementation contract for *this* repo — it records which parts are adopted
verbatim, which are deliberately simplified, and which decisions the source
left open. Where this doc and the notes disagree, this doc wins for
implementation purposes.

## How to Read This Doc

| Section | Question it answers | Read it when |
| --- | --- | --- |
| Runtime view | what happens to one request | first time |
| Design positions | why the shape deviates from the notes | reviewing/challenging the architecture |
| Module map, runtime objects, decision tables | the M0 skeleton in code terms | writing `src/orchestrator` |
| Registry, capability protocol | how a capability plugs in | adding capability #2 |
| Persistence, CLI | how to run and replay | operating it |
| Milestones | build order and per-step proof | starting work |
| Not doing | what is fenced off | tempted to add scope |

## Runtime View

```text
               ┌───────────────────────── harness (deterministic code) ─────────────────────────┐
user request ─▶ safety gate ─▶ normalize (rules) ─▶ [LLM #1: flash interpret] ─▶ assess (signals
               │                                                              ─▶ confidence state)
               │                     ┌── routing table (first match) ◀── attempt counters
               │                     │                                              (on the envelope)
               │   reject / clarify / handoff ◀──┐        execute_capability ─▶ policy check ─▶ capability.run()
               │                                 │                                        │        (rag is #1)
               │                         handoff packet                            [LLM #2: stronger-model
               │                         (persisted, exported as                   reinterpret ── back to assess]
               │                          markdown — no ticketing)                   │
               └──▶ validation table ─▶ final outcome ◀───────────────────────────────┘

        every step: one of the 7 typed objects written to SQLite + one event appended
        `orchestrate replay <request_id>` reconstructs the whole decision path
```

Two flow paths only — everything else is a table outcome. The loop is
central: execution and validation never branch; they return structured
results to `routing`, which is the single control authority.

## Design Positions

Numbered so code and tests can cite them ("DP-3").

### DP-1. Lives in this repo, on existing infrastructure

No new dependency is introduced that `pyproject.toml` does not already have
(pydantic-style contracts via dataclasses + hand-rolled validation or
pydantic if already present — see Tech choices). LLM access is exclusively
via `src/llm_client`; storage via `src/storage`; the first capability wraps
`src/rag`. AGENTS.md gotcha applies: add `src/orchestrator` to the hatch
`packages` list and re-run `uv pip install -e .`.

### DP-2. Chinese input is first-class

- Deterministic normalization owns the alias/shorthand table (Chinese
  nicknames, short names, mixed-language entities) — the notes' Stage 2 is
  where Chinese handling earns the most, because it is cheap and auditable.
- Retrieval-side CJK behavior is inherited, not reinvented: the RAG
  capability goes through `rag query`, and `storage.fold_cjk` /
  `storage.match_expr` are already the shared solution (see
  `sqlite-fts5-cjk-quirk` notes in `docs/storage-usage-guide.md`).
- The flash-interpretation prompt is language-agnostic; `original_input.text`
  is never translated (input preservation rule, CH01 Stage 1).

### DP-3. Human handoff = a persisted object + markdown export, nothing more

No ticket system, no email, no queue service. `handoff-list` and
`handoff-export <handoff_id>` produce a scan-friendly markdown packet (the
six CH01 packet sections mapped onto the handoff object's fields). This is
enough to close the loop later with any external system, and keeps the first
version dependency-free. *Amended 2026-09-22 (G-2):* a worklist table may
*consume* packets (claim/resolve ownership, packets never mutated, no
state-machine edge) without breaking this freeze — the freeze was about
the packet's semantics, not about leaving them unowned.

### DP-4. Budget defaults are relaxed for a CLI + retrieval runtime

The notes' `max_wall_clock_ms: 8000` was scoped to a chat UI. Defaults here
(and their rationale) live in one config module:

```yaml
execution_budget:          # first-version defaults, per request
  max_total_loops: 6       # unchanged
  max_tool_calls: 6        # 4 → 6: rag capability internally chains retrieve→rerank→answer
  max_reinterpretations: 2 # unchanged
  max_execution_retries: 2 # unchanged
  max_clarification_turns: 2 # unchanged
  max_model_escalations: 1 # unchanged
  max_wall_clock_ms: 30000 # 8 s → 30 s: lexical query + LLM answer generation
  max_llm_calls_per_day: 200  # 05c quota gate — per user per UTC day, not per request
```

These live on the request envelope, not in memory variables (DP-8), and a
CLI process boundary must not silently reset them.

### DP-5. Domain routing is demoted to registry metadata in v1

CH02 resolves tools in two levels (domain, then capability). With a
single-digit capability count, a separate domain-routing step forwards
parameters without changing responsibility — the exact "ceremony, not
modularity" the source set itself warns against. So:

- `domain_scope` stays a required field on every catalog entry (ownership,
  future ACL, future attribution).
- Routing picks a capability directly; `selected_domain` on the routing
  decision object is filled from the entry, not decided by a step.
- Promotion condition is explicit: when entries from ≥ 2 domains with
  overlapping `task_types_supported` make the single-level table misroute,
  add a domain sub-table. Object schema does not change.

### DP-6. No execution-graph planning (CH02 step 8A) in v1

The model never proposes tool DAGs here. A capability may internally run a
fixed mini-pipeline (the rag adapter: retrieve → assemble → answer) and the
catalog entry declares that pipeline's name, but the orchestrator sees one
`run()` call and one structured result. `execution_plan.tool_calls` in the
execution record is recorded *after the fact* from what the capability
reported, purely for observability.

### DP-7. Decision tables are data with row ids, evaluated by one interpreter

Every row of CH01 routing / CH02_03 execution / CH02_03 validation /
CH02_02 fallback is a tuple `(row_id, condition, action, reason_code)`,
conditions being pure callables over an assessment/result object. One
first-match interpreter per table. Rationale: CH04's claim — "a decision
table is only as strong as the tests covering its rows" — becomes mechanically
checkable (a test suite enumerates row ids and demands ≥ 1 case each), and
row order (which the notes call deliberate) is reviewable in a diff.

### DP-8. All state that routing needs lives on the envelope

Budgets, counters, `current_status`, `selected_capability`, clarification
history refs. Consequences: the clarify loop survives the CLI process
boundary (`ask --resume <request_id> "answer"`), budgets are structurally —
not decoratively — "visible to routing at all times", and replay reads one
root object.

### DP-9. Confidence is a state first, a score only for calibration

`assess` emits one of `clear / weak_but_usable / ambiguous / unsafe /
blocked` by pattern over the four signal groups (CH02_03). The
`aggregate_score` is recorded but never consulted by any decision-table
condition — the notes' ordering rules (policy beats confidence, ambiguity
beats score) are guaranteed by construction, and threshold calibration later
happens against labeled golden cases, not against the score formula.

### DP-10. `proceed_conservative` has real semantics on the rag capability

Broaden retrieval (top-k × 1.5, relax soft filters), narrow the answer
(one paragraph, evidence-bound), force a disclaimer line, and mark the final
outcome `partial_answer` if validation flags uncovered sub-questions. It is
not a second `proceed`.

## Tech Choices

| Concern | Choice | Why |
| --- | --- | --- |
| Object model | pydantic v2 models in `contracts.py` | the seven objects *are* the public schema; validation + JSON (de)serialization for free; rag/research_agent already use pydantic-style contracts |
| Persistence | one SQLite file at `data/orchestrator/orchestrator.db` via `src/storage/sqlite.py` (`ensure_columns`, WAL pragmas) | repo rule: default paths anchored in `utils.paths.data_dir("orchestrator")` |
| LLM | `llm_client.chat_json()` only; two call sites (`interpret`, stronger-model re-interpret) + clarification question generation inside the builtin escalation capability | AGENTS.md one-rule |
| Registry | tracked YAML at `config/orchestrator/capabilities.yaml`, loaded and validated at startup | catalog entries are domain-team-owned config, not platform code — YAML keeps the diff readable for owners who don't touch `src/` (same "tracked declaration" posture as `skill_manager.sources.UPSTREAMS`, different edit audience) |
| Concurrency | none in v1 — synchronous state machine, `asyncio.run` scoped inside capability adapters | the control loop is serial by definition; rag's `TaskQueue` stays behind the adapter boundary |
| Web layer | none in v1 | CLI first, per file_manager/rag precedent; a FastAPI shell is a later projection of the same store |

## Module Map

```text
src/orchestrator/
  __init__.py
  contracts.py           # 7 runtime objects + ConfidenceAssessment + CapabilityResult (pydantic)
  safety.py              # input gate: deterministic pattern table → allow|constrain|clarify_scope|refuse|handoff
  normalize.py           # deterministic conditioning: alias table, short-name recovery; returns traceable rule hits
  interpret.py           # flash-model interpretation via llm_client → InterpretationRecord; strict-JSON schema = contracts
  assess.py              # 4 signal groups → ConfidenceAssessment (state, not formula) — DP-9
  policy.py              # 4 decision tables as row data + first-match interpreter — DP-7
  runtime.py             # state machine, control-loop rule (§Control Loop), budget comparison; sole writer of current_status
  registry.py            # load + validate capabilities.yaml; lookup by task_type/use_when; no domain step — DP-5
  execution.py           # policy check → capability.run() → ExecutionRecord; maps failures to structured codes
  capabilities/
    protocol.py          # Capability protocol (typing.Protocol, not ABC — structural typing keeps adapters dumb)
    builtin.py           # clarification generation + handoff packet building (escalation family)
    rag.py               # adapter over src/rag query path — DP-6, DP-10
  store.py               # requests + events tables (via src/storage); snapshot read/write, event append
  golden.py              # golden-case runner: replay cases, diff emitted decisions (expected vs model-proposed vs harness-selected)
  config.py              # budget defaults (DP-4), thresholds per CH04 table, capability registry path
  cli.py                 # `orchestrate` entry point
  py.typed
```

## Runtime Objects (M0 contract)

Field lists are CH02_01 translated to pydantic; required-vs-recommended
follows the source exactly. Only repo-specific notes are written out.

1. **RequestEnvelope** — `request_id`, `session_id`, `user_id`,
   `entrypoint`, `timestamp_start`, `original_input{text, attachments,
   locale}`, `context{chat_history_ref, prior_request_refs, tenant_id}`,
   `policy_context{identity_tier, permission_profile, risk_profile,
   data_scope}`, `execution_budget` (DP-4), `attempt_counters`,
   `state{current_status, selected_domain, selected_capability,
   final_outcome_ref}`.
   *All IDs are ULID-style strings (`req_`, `int_`, `route_`, `exec_`,
   `out_`, `handoff_` prefixes) so ordering is time-sortable in SQLite.*
   `original_input.text` is write-once by contract (frozen model or
   field validator rejecting changes after creation).

2. **InterpretationRecord** — `interpretation_id`, `request_id`,
   `timestamp`, `normalized_query`, `task_type`, `candidate_domains`,
   `target_entity_guess`, `requested_attributes`,
   `deterministic_signals{alias_hits, top_match_score, top2_gap, rule_hits}`,
   `model_signals{model_name, confidence, ambiguity_flags,
   alternative_interpretations}`, `interpretation_summary`.
   Regenerable per clarification turn; every version is stored (append,
   never update).

3. **RoutingDecision** — `routing_decision_id`, `request_id`,
   `interpretation_id`, `decision` (9 canonical values),
   `decision_reason{primary, supporting_signals, table_row_id}`
   (`table_row_id` is the DP-7 addition: which row fired),
   `selected_domain`, `selected_capability`, `fallback_capability`,
   `constraints`, `next_action{type, payload}`.

4. **CapabilityExecutionRecord** — `execution_id`, `request_id`,
   `routing_decision_id`, timestamps, `domain`, `capability_name`,
   `capability_version`, `tool_bundle_loaded`,
   `policy_check{allowed, permission_profile, risk_decision}`,
   `execution_plan` (observational only — DP-6),
   `result{status, output_ref, evidence_refs, confidence_signals}`,
   `duration_ms`. Written on failure too; that record *is* the audit trail.

5. **FinalOutcome** — `final_outcome_id`, `request_id`, `outcome_type`
   (6 canonical values), `user_response_ref`, `validation_summary{relevance,
   completeness, grounding, policy_compliance}`, `used_execution_ids`,
   `fallback_history`.

6. **HandoffPacket** — `handoff_id`, `request_id`, `reason{code, summary}`,
   `conversation_context`, `current_interpretation`,
   `attempt_history{routing_decision_ids, execution_ids}`, `budget_state`,
   `recommended_next_step`. Markdown export renders exactly the six CH01
   packet sections from this object (DP-3).

7. **CapabilityCatalogEntry** (config-time) — the CH02_01 YAML schema, with
   all 11 required fields enforced at registry load (`name`, `owner`,
   `domain_scope`, `capability_version`, `rollout_status`, `use_when`,
   `avoid_when`, `tool_schema_bundle`, `output_contract`, `fallbacks`);
   `loading_mode` is accepted but only `single_capability` is implemented
   in v1.

Plus two small internal value objects, not persisted as first-class rows:
**ConfidenceAssessment** (assess output, embedded in the routing decision's
reason) and **CapabilityResult** (protocol return, embedded in the execution
record).

## Decision Tables

One module, four tables, one interpreter. Rows carry ids so golden cases and
coverage tests can name them ("route_r4_clarify_close_candidates").

- `ROUTING_TABLE` — 10 rows, CH01 order preserved (policy/handoff hard stops
  first; clarify before escalation; `handoff_human` as the terminal default
  row). Row 10 (`route_r10_quota_exhausted`) is a 2026-09-22 amendment —
  see below.
- `EXECUTION_TABLE` — 7 rows, CH02_03.
- `VALIDATION_TABLE` — 7 rows, CH02_03 (`accept | partial_answer | retry |
  switch_capability | clarify | reject | handoff_human | failed`).
- `FALLBACK_TABLE` — 5 rows, CH02_02 (cap reached → still-legal actions).

Interpreter contract: takes ordered rows + a signal object; first fully
matching row wins; the emitted `RoutingDecision.decision_reason.table_row_id`
records the row. No table may call an LLM. Tests: for every `row_id`, at
least one golden case must fire it (enforced by a parametrized test over the
row registry).

### Amendment 2026-09-22: the quota row (05c step 2)

`route_r10_quota_exhausted` — predicate `signals.quota_exhausted`, action
`handoff_human`, reason `daily_llm_call_quota_exhausted`. Position is
between r2 and r3: a user out of daily calls must not reach clarify (r3/r4
spend a later turn) or escalation (r5/r6 spend tokens now); the id stays
append-only because row ids are audit references once shipped.

Semantics fixed by this amendment:

- the budget line is `execution_budget.max_llm_calls_per_day` (default 200,
  sized by `docs/orchestrator/05c-cost-gate.md`'s cost model); consumption
  is `attempt_counters.llm_calls` — one per front-half pass that really
  called the model (the deterministic short-circuits report
  `model_name="none"` and cost nothing) plus whatever a capability reports
  via `CapabilityResult.llm_calls` (the capability is the only party that
  knows its own spend; the runtime only adds it — DP-6 posture);
- *today for this user* is `Store.llm_calls_today(user, now)` —
  `json_extract` over the user's envelopes created in the current UTC day,
  excluding the in-flight request (its counter lives on the live envelope,
  so including both would double-count — DP-8);
- quota is deliberately **not** a `CapReached`: caps feed r2 through
  `any_budget_exhausted` and would swallow the quota into a misleading
  audit row ("escalation_budget_exhausted"), and `FALLBACK_ROWS` needs no
  new entry because the row's own action is already terminal;
- model escalation consumes quota — g19 proves the walk r6→r10; g18 proves
  the exhausted path. Goldens run as per-case users
  (`golden:<case_id>`) so one case's spend cannot leak into the next.

## Governance Records

Decisions the code cannot answer by itself, decided here (on schedule)
rather than in a hurry. `docs/orchestrator/05d-lifecycle-governance.md`
carries the triggers and the build consequences; the contract owns the
decision.

### Record G-1: erasure vs. append-only (05d step 1, 2026-09-22)

**Question:** GDPR/PIPL right-to-be-forgotten vs the replay guarantee and
append-only objects. User text today lives on every durable surface:
`original_input.text` and `normalized_query` in the envelope, every
`runtime_objects.payload_json` (interpretations echo it, the handoff
packet embeds conversation context), and `events` payloads
(`request_captured`, `clarification_received`). The architecture
currently picks neither guarantee.

**Decision: crypto-erase** — per-user data keys wrap the text surfaces;
erasure deletes the key, which renders every stored copy unrecoverable
with zero row edits.

- Why: it leaves both load-bearing properties intact untouched — the
  append-only store (DP-8) and the row-id audit (DP-7, whose rung-B
  compliance value is exactly "these rows fired, in this order"). The
  tombstone alternative (blank text fields in place) is cheaper to run
  but *edits* history, so the replay story would have to redefine what
  a blanked row proves — the audit artifact becomes conditional on the
  erasure ledger.
- Cost accepted: key-management infrastructure (key derivation, custody,
  a deletion path that is itself auditable). Deferred, because —
- **Effective date: the first EU (or PIPL-scope) tenant, with legal
  sign-off at that date.** This record fixes the *shape* (crypto-erase,
  key-deletion, zero row edits, envelope contract unchanged — the
  encryption lives at the store's serialization boundary, e.g. one
  `data_key_id` column on `requests`), not the key-management product
  choice. Until the trigger, erasure stays under "Explicitly Not Doing".

### Record G-2: the handoff worklist (05d step 2, 2026-09-22)

A fourth table, `handoff_tickets`, amends "Persistence" as a pure
**consumer** of persisted packets — DP-3's "packet + export, nothing
more" is deliberately kept: packets are only ever *read* (claim derives
its `request_id` from the stored packet and refuses unknown ids; no
write path to `runtime_objects` exists here), and worklist rows never
enter the state machine — the absent `handoff → routing` edge remains a
DP-3 contract conversation, not coded around.

Semantics fixed by this record:

- **open is the absence of a row**: creating a packet can never race a
  claim, and pre-worklist packets need no backfill; the worklist is the
  join of stored packets onto ticket rows;
- the lifecycle is claim → resolve: resolving an open ticket is refused
  (an unowned resolution is the black hole with a timestamp), only the
  claimant may resolve, and `--reassign` makes takeover explicit;
- assignee is operator-self-asserted at the CLI until 05b's identity
  lands on the service host, and tenant scoping of the worklist ride
  the same 05b step — the table carries `request_id`, so the join to a
  future tenant column is already there.

## Control Loop

Every return to `routing` runs CH02_02's four steps, in code:

```text
1. result = latest InterpretationRecord | CapabilityExecutionRecord | ValidationSummary
2. counters = envelope.attempt_counters.increment(branch_of(result))
3. legal   = counters <= envelope.execution_budget          # DP-8
4. decision = ROUTING/EXECUTION/FALLBACK table evaluate(assessment, legal)
```

States: `captured → interpreting → routing ⇄ (awaiting_clarification |
executing → validating) → (completed | handoff | rejected | failed)`, with
the two recovery edges `handoff/failure → routing` from CH02_02's diagram.
One active state per request; every transition appends an event with
timestamp + trigger. `clarify` and `handoff` end the current *turn*, not the
session — the process is expected to exit after either (DP-8).

Events (first version, verbatim from CH02_02): `request_captured`,
`interpretation_created`, `routing_decided`, `clarification_requested`,
`clarification_received`, `capability_execution_started`,
`capability_execution_completed`, `validation_completed`,
`human_handoff_created`, `request_completed`, `request_rejected`,
`request_failed`.

## Capability Registry & Protocol

`config/orchestrator/capabilities.yaml` — first two entries:

```yaml
- name: promotion_knowledge_query        # wraps `rag query` on the corpus
  owner: rag
  domain_scope: customer
  capability_version: 0.1.0
  rollout_status: active
  use_when: [question about policy/promotion documents, needs cited evidence]
  avoid_when: [exact structured field lookup, action request]
  tool_schema_bundle: [rag_query]
  loading_mode: single_capability
  output_contract:
    required_fields: [answer_markdown, evidence_refs, outcome_kind]
    optional_fields: [uncertainty_note]
  confidence_signals: [retrieval_agreement, grounding_coverage, top_score]
  validation_rules: [grounding_coverage_min]
  cost_profile: medium
  latency_profile: medium
  risk_profile: low
  fallbacks: [human_handoff]

- name: human_handoff                    # builtin escalation family
  owner: platform
  domain_scope: global
  ...
```

Protocol (one method, one dataclass):

```python
class Capability(Protocol):
    def run(self, ctx: CapabilityContext) -> CapabilityResult: ...
# ctx:  normalized_query, requested_attributes, policy_context,
#       constraints (from routing decision), request/session ids
# CapabilityResult: status(success|weak|failed), code, output,
#                   evidence_refs, confidence_signals, tool_steps
```

Rules inherited from CH02 and enforced in `execution.py`: capabilities never
call each other; capabilities never see or mutate the envelope; a capability
returns structured codes (`dependency_timeout`, `permission_denied`, …) and
does not decide its own retry; read capabilities are policy-checked exactly
like write ones.

The rag adapter maps CH03_04's five outcomes onto
`CapabilityResult.status` + `code` (`answered → success`,
`partial → weak`, `insufficient_evidence → weak + code=grounding_insufficient`,
`clarification → weak + code=user_constraint_missing`,
`escalated → failed + code=policy_escalation`) — routing tables, not the
adapter, turn those into decisions.

## Persistence

Two tables in `data/orchestrator/orchestrator.db`:

- `requests(request_id PK, session_id, user_id, status, envelope_json,
  created_at, updated_at)` — the envelope is authoritative; the six other
  per-request objects live in `runtime_objects(object_id PK, request_id FK,
  kind, seq, payload_json, created_at)` (append-only; `seq` reconstructs
  order).
- `events(id INTEGER PK, request_id, event, payload_json, ts)` — derived
  projection for replay/monitoring; snapshots remain source of truth.
- `handoff_tickets(handoff_id PK, request_id, assignee, claimed_at_ms,
  resolved_at_ms, resolution)` — the worklist (G-2): consumers of
  packets, outside the state machine; no row means open.

Replay = read objects for `request_id`, re-render the decision path; golden
regression = re-run a case through the runtime and diff emitted
RoutingDecision/FinalOutcome against stored expectations (CH04's
comparison-by-object, not by prompt internals).

## CLI Surface

```text
orchestrate ask "春晖省钱卡怎么用"            # one turn; prints answer | question | handoff notice
orchestrate ask --resume req_01J... "第二个"  # clarification turn, budget survives (DP-8)
orchestrate status [req_id]                   # request list / one-request timeline
orchestrate replay req_id                     # decision path reconstructed from store
orchestrate handoff list
orchestrate handoff export <handoff_id> --out packet.md
orchestrate handoff worklist [--all]           # open/claimed/resolved (G-2)
orchestrate handoff claim <handoff_id> --assignee ops [--reassign]
orchestrate handoff resolve <handoff_id> --assignee ops --note "..."
orchestrate golden run [--class routing|permission|...]
orchestrate registry check                    # validate capabilities.yaml against the entry schema
```

`orchestrate ask` is the only LLM-hitting command; `golden run` uses fakes
for M0-era tests and live models behind the same skip convention as
`test_summarize_live` (`LLM_API_KEY` unset ⇒ skip).

## Milestones

Each ships tests + a `docs` update; each ends live-verified on this machine
or logs the debt in root `TODO.md` (house rule).

| # | Deliverable | Proof |
| --- | --- | --- |
| **M0 skeleton** | `contracts` + `store` + `policy` (4 tables) + `runtime` + fake capability + CLI `status/replay/golden run` | **zero LLM calls** and still passes: per-row routing tests, loop-budget wall tests, fallback-after-cap, handoff required-field checks (CH04 classes 4 & 7 partial) |
| **M1 front half** | `safety` + `normalize` (alias table w/ Chinese) + `interpret` (llm_client) + `assess` + golden-case format | safety-gate, routing, clarification test classes green on a starter golden set (≥ 5 cases/row is the goal, ≥ 1 is the gate) |
| **M2 first capability** | registry YAML + `capabilities/rag.py` over the shipped rag query path + validation table live | end-to-end on a real corpus: ask → (clarify) → proceed → cited answer; `partial_answer` and DP-10 conservative path exercised; grounding-coverage threshold from CH04 defaults |
| **M3 second capability** | structured lookup (FTS5 over existing metadata, e.g. file_manager records) + `switch_capability` reachable | proves the plug-in claim: registry entry + adapter, zero runtime changes; execution-table tests for retry/switch paths |

### Implementation notes (M0, 2026-09-22)

Decisions the contract left open, now fixed in code:

- **Fallback legality is branch-governed.** The control-loop sketch says
  "legal = counters <= budget"; applied literally, a spent *escalation* cap
  would veto unrelated actions like `proceed`. Correct semantics (CH02_02's
  per-cap "still-legal" lists read naturally this way): a spent cap forbids
  only the action that consumes that cap's counter — `stronger_model`
  (reinterpretation/model-escalation), `clarify` (clarification), `retry`
  (execution-retries). Terminal/progress actions stay legal; total-loop cap
  and wall clock remain the r2 hard stop. See `runtime._apply_fallbacks`.
- **Open Question 2 resolved without a dependency:** `ids.py` emits
  Crockford-base32 `ms-timestamp + random` strings, lexicographically
  time-sortable (`req_01J...`).
- **Open Question 3 resolved:** `AttemptCounters` is a sibling model of
  `ExecutionBudget` on the envelope; comparisons happen only in
  `assess.caps_reached`.
- **Store ordering:** `runtime_objects.seq` is global per request (not per
  kind) so `replay` reconstructs the true interleaved decision path.
- **Event detail:** `validation_completed` carries `execution_row_id` and
  `validation_row_id` separately — the execution table's opinion must
  survive replay even when the validation row decides the action.
- The M0 registry is static in-code (`registry.load_static`); the YAML
  promotion still lands at M2 as planned.

### Implementation notes (M1, 2026-09-22)

- **The gate runs before the model, and hard stops cost zero tokens:**
  `interpret.LlmFrontHalf` evaluates `safety.evaluate(original text)` first;
  `refuse`/`handoff` short-circuit with a fully-formed InterpretationRecord
  (`model_name="none"`) and no model call. `clarify_scope` does *not*
  short-circuit — the model's reading still enriches the interpretation, but
  the gate's question wins over the model's suggestion.
- **normalize scores are specificity, not probability:** a canonical's score
  is `len(matched alias) / len(its longest alias)` (full name 1.0, nickname
  less); `top2_gap` compares the two best *distinct* canonicals, which is
  what `assess` reads as close_candidates. Bare short names ("报销") are
  deliberate rows — they make the gap scenario reachable and the rule_hits
  traceable (`alias:费用报销系统<-报销`).
- **The model proposes through one strict schema:** `ModelInterpretation`
  (pydantic, extra=ignore) is the complete set of fields the flash model may
  assert — task_type, attributes, confidence, ambiguity, a clarification
  question, constraints. Anything outside it is dropped, so a prompt
  injection cannot address a harness field. The deterministic pass owns
  `normalized_query`; the model never rewrites text (input preservation).
- **Model roles are env-overridable, escalation is a rename:**
  `config.Models.FLASH` / `Models.ESCALATED` (empty ⇒ llm_client default);
  `escalated=True` only swaps the `model=` argument, the prompt adds one
  caution line. One interpret call = at most one model call, `asyncio.run`
  scoped inside the front half (the loop itself stays synchronous).
- **`ask` failure surface:** a front-half exception exits 1 with
  `ask failed: …` on stderr — the runtime never half-answers.
- Live-verified 2026-09-22 against the real provider: `orchestrate ask
  "春晖省钱卡怎么续费"` completed through interpret → r7 → echo;
  `ask "帮我查一下张三的身份证号"` rejected via s2 with no model call.

### Implementation notes (M2, 2026-09-22)

- **Two registry views, deliberately separate:** `load_default()` reads
  `config/orchestrator/capabilities.yaml` (every `REQUIRED_FIELDS` key must
  be present in the raw entry — pydantic defaults would hide a half-written
  config) and binds implementations by name; `load_static()` stays the
  code fixture for tests/golden, so regression never needs a built corpus.
  `rag query`'s own CLI and the adapter share `data/rag/kb.db` (the rag
  default location) — no second index.
- **The adapter translates, never decides:** rag's five explicit outcomes
  map to the structured vocabulary — clarify → `weak/user_constraint_missing`
  (reaches e5), insufficient/escalated → `weak/insufficient_evidence`,
  any crash → `failed/dependency_unavailable`. And when e5 asks, the
  *capability's* clarification text is the question shown (runtime seeds
  `turn.out.clarification_question` from the result when the front half had
  no question) — routing stays the only reinterpret authority, but the user
  hears what actually went missing.
- **`citations` is a required output field, and the adapter withholds it on
  partial answers** — that (not an invented status) is what makes validation
  v6 `partial_answer` reachable: useful text + missing field + low risk.
  `grounding_coverage` = claims with valid refs / claims; v3 fires below the
  0.5 floor before v6 gets a chance, so weakly-grounded answers hand off
  rather than partial-answer.
- **DP-10 is one line where it matters:** `conservative` + `topk_factor`
  from the routing decision's constraints widens `retrieve(k=ceil(5*1.5))`
  inside the adapter and nowhere else.
- Live proof 2026-09-22 (corpus: 4 synthetic Chinese policy docs under
  `data/rag_inbox`, built by `rag build`): `ask "春晖省钱卡每月抵扣上限是多少"`
  → exec_e6 → val_v5_accept, stored execution carries `grounding_coverage
  1.0` + two anchored citations with doc ids and pages.

### Implementation notes (M3, 2026-09-22)

- **The plug-in claim held — with one signal-derivation correction.** Adding
  `structured_lookup` as rag's *runnable* fallback changed nothing in the
  tables or the loop, but it exposed that `result_weak` had been derived as
  `status == weak`, which silently included `user_constraint_missing`: the
  moment an alternate capability existed, e4 `switch_capability` outran e5
  `clarify` and a missing user constraint got "fixed" by a different tool.
  The runtime now derives `result_weak = weak and code !=
  "user_constraint_missing"` — a user constraint is not switchable weakness.
  Table rows are untouched; only signal ownership moved.
- **Wall clock budgets machine work, not human answer time** (DP-4's "8 s →
  30 s: lexical query + LLM answer" is about latency, and the CLI clarify →
  resume flow crosses a human pause by design). `_transition` — already the
  sole writer of `current_status` — now also accounts wait time: entering
  `awaiting_clarification` stamps `StateRefs.wait_started_ms`, leaving it
  folds the gap into `ExecutionBudget.wall_clock_paused_ms`, and
  `assess.wall_clock_exceeded` subtracts that. Counters stay charged
  (a clarification turn still spends `clarification_turns`); only the clock
  stops. A stalled *machine* loop still dies at 30 s.
- **Proposal-boundary leniency:** flash models write *what is missing*
  ("具体活动标识") into `missing_required_constraint` instead of a boolean.
  `ModelInterpretation` now coerces any non-negating string to True at the
  boundary rather than bouncing a repair round-trip — repair costs 5–10 s of
  the wall clock per call and the model was saying the same thing either way.
  Booleans the harness *acts* on are unaffected; only the proposal shape is
  forgiving.
- **M3 consumed a domain and found a latent shared-layer bug (its real
  dividend):** feeding a whole normalized sentence to file_manager's search
  produced units like `Activity_Brief_Cold_Email.pdf，` — `storage.fts.
  token_expr` emitted them unquoted and FTS5 died with `syntax error near
  "."`, which callers saw as a dead dependency. Non-word-like folded units
  are now quoted phrases (word-like ASCII and CJK units stay bare, prefix
  wildcards unchanged); the file_manager UI's own dotted queries were broken
  the same way before anything orchestrator-side existed.
- **Lookup adapter posture:** zero hits is `weak/insufficient_evidence`
  (the tables decide the consequence, not the adapter); an empty query is
  `weak/user_constraint_missing`; a dead file db is
  `failed/dependency_unavailable`. Records are data, not grounded claims —
  the entry carries **no** `grounding_coverage_min` and requires
  `[answer_markdown, records]`. It declares `task_types: ["*"]` but sits
  *after* rag_query in the YAML, so `select()` never routes to it directly:
  it is reachable *only* as the declared fallback — which is exactly what
  makes e4/v3 real instead of theoretical.
- Live proof 2026-09-22 (real `data/file_manager/file_manager.db`, 3
  records): `ask "冷邮件活动的简报 PDF 是谁上传的"` → r3 clarify → resume
  with a file-library scope (25 s human gap, wall clock unharmed) → r7
  proceed → rag weak `insufficient_evidence` in 8 ms → **exec_e4 switch** →
  lookup success (relaxed match on the real filename, uploader attributed)
  → e6 → val_v5_accept → completed.

## Explicitly Not Doing (v1)

- Long-term agent memory (the notes defer it; boundaries undefined — stays deferred).
- Erasure implementation — the *decision* is recorded (G-1: crypto-erase,
  effective at the first EU/PIPL tenant); the code stays unbuilt until the
  trigger, so today no stored user text can be deleted.
- Multi-tenant auth or real permission backends: `policy_context` fields
  exist and are *threaded through every object*, but the only enforcement is
  "everything readable, profile recorded" — the seam is there, the lock is not.
- Domain routing as a step (DP-5), execution-graph planning (DP-6).
- HTTP service, queue, nightly summarization job, alerting.
- `staged_supervisor` / `primary_plus_fallback` loading modes.
- Confidence calibration beyond the decision tables themselves; thresholds
  are config constants until labeled golden cases justify moves.

## Open Questions

1. ~~Second demo domain and corpus for M3~~ — resolved at M3: file_manager
   metadata (the zero-cost option) proved the switch paths live, and feeding
   it surfaced the `token_expr` quoting bug in the shared layer (see
   Implementation notes). A small HR-style fixture for honest permission
   profiles stays open — it is an *enforcement* gap (Explicitly Not Doing),
   not a registry gap.
2. ~~ULID library vs time-sortable hex ids~~ — resolved at M0 (see
   Implementation notes).
3. Whether `attempt_counters` belongs *in* `execution_budget` or beside it
   on the envelope (CH02_01 shows limits in the budget, counters as runtime
   state; DP-8 forces both onto the envelope — keep them as two sibling
   dicts, limits and usage, rather than interleaved). — resolved at M0.
