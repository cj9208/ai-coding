# Orchestrator — Implementation Guide

What `src/orchestrator` actually is today: 20 modules, ~3700 lines, M0–M3
complete. This document walks the shipped code — responsibilities,
constants, and the derivation rules that only exist *in* code. When the
contract (`../orchestrator-design.md`) and this disagree, this document is
right about the code and the contract is right about the intent; the
contract's per-milestone Implementation Notes are the official record of
*decisions*, this is the map of *wiring*.

## Module map

```text
 THE CONTRACTS (one module defines what all others exchange)
 contracts.py    635   seven runtime objects + signal/value objects,
                       all enums; _Contract = strict pydantic base
 protocol.py      40   Capability + FrontHalf Protocols (structural, not ABC)

 THE HARNESS
 runtime.py      865   Orchestrator: state machine + control loop,
                       sole writer of current_status (_transition)
 policy.py       286   3 decision tables + fallback rows, as Row data
 assess.py       180   envelope+output -> signal objects; caps; wall clock
 registry.py     162   Registry + load_yaml / load_default / load_static;
                       config_hash_of names the artifact (05d)
 store.py        503   4-table SQLite persistence (via src/storage) —
                       the 4th is the 05d handoff worklist (tickets
                       consume packets; open = no row; never a mutation)
 ids.py           28   Crockford base32 ms+random ids, lexicographically
                       time-sortable

 THE FRONT HALF (M1)
 interpret.py    271   LlmFrontHalf: gate -> normalize -> (fast path | flash
                       chat_json), all three assets selected by the
                       caller's locale pack
 safety.py       ~85   SafetyRow/Verdict shapes + first-match + locale-aware
                       evaluate (late pack lookup; no silent allow on miss)
 packs/          05b   per-locale front-half assets: base.py (shapes),
                       zh.py (s1..s5+s0 + ALIASES + prompt, pre-05b verbatim),
                       en.py (first tranche, IGNORECASE rows, no aliases yet)
 normalize.py    103   specificity scoring, traceable rule hits
                       (default ALIASES now come from the zh pack)
 config.py        79   Budget / Thresholds / Models / FastPath defaults,
                       env overrides

 THE CAPABILITIES (M2/M3)
 capabilities/rag.py     188   RagQueryCapability over src/rag store
 capabilities/lookup.py  155   StructuredLookupCapability over
                               file_manager metadata search
 capabilities/builtin.py 130   handoff packet + six-section markdown export
 capabilities/fake.py    117   EchoCapability + FakeFrontHalf (M0 world)

 THE REGRESSION SURFACE
 golden.py       215   case loader + scripted replay + decision diff
 cli.py          321   orchestrate: ask/status/replay/handoff
                       (list|export|worklist|claim|resolve)/golden/registry
```

Imports are one-directional: `cli` → `runtime`/`registry`/`interpret` →
tables/assess/store → `contracts`; adapters under `capabilities/` import
contracts + *other packages* (rag, file_manager, storage) but never the
runtime. `runtime.py` imports no LLM library and no domain package — the
only seams in are the injected `FrontHalf` and `Registry`.

## The control loop, exactly as shipped

`_drive` is a `while True` over the persisted status; every iteration
starts with the wall-clock check (the r2 hard stop). The four steps of the
contract's §Control Loop live in `_route`:

```text
1. read latest state          (envelope + turn scratch)
2. increment the right counter (total_loops here; retries/escalations at
                               the edge that consumes them)
3. caps_reached -> legality    assess.caps_reached -> _apply_fallbacks
4. table decides among         ROUTING_TABLE.decide(signals) -> row id
   legal actions               -> persist RoutingDecision -> act
```

`_execute` never trusts a capability: exceptions become
`failed/capability_crashed`, the `CapabilityExecutionRecord` captures the
result *and* the tool steps it reported (observational — DP-6), and only
then does `_validate` run the execution table; `accept` is the only action
that consults the validation table (v1..v7 can downgrade but never upgrade).

Three **derived-signal rules** in `_validate`/`_transition` encode policy
that the tables alone cannot state — they are the M3 notes condensed:

| Rule | Code | Why |
| --- | --- | --- |
| user constraint ≠ switchable weakness | `result_weak = status==weak and code != "user_constraint_missing"` | once rag gained a runnable fallback, e4 would have outrun e5 forever |
| human time is not machine time | `_transition` stamps `state.wait_started_ms` entering `awaiting_clarification`, folds the gap into `budget.wall_clock_paused_ms` leaving it; `assess.wall_clock_exceeded` subtracts | resume-after-clarify is otherwise dead at 30 s, and the budget's purpose is latency, not a race against reading comprehension |
| the proposal boundary is forgiving | `ModelInterpretation._coerce_bool`: any non-negating string in the two boolean fields → True | a repair round-trip costs 5–10 s of that same budget; the model writing "具体活动ID" into a boolean field *is* saying true |

## The four tables

`policy.py` — all row ids listed so tests and docs can enumerate them
(the coverage invariant: ≥ 1 golden case fires each row).

- **ROUTING_TABLE** r1..r10: reject(policy) → handoff(budget) →
  handoff(quota, 05c r10) → clarify(missing constraint) → clarify(ambiguity)
  → stronger_model ×2 → proceed → proceed_conservative →
  handoff(default r9; r10's id extends the numbering, its position is
  between r2 and r3 — audit ids are append-only).
- **EXECUTION_TABLE** e1..e7: reject(denied) → handoff(retries spent, no
  alternate) → retry(transient) → switch(weak + alternate) → clarify(user
  constraint) → accept(grounded, hands off to validation) → handoff(default).
- **VALIDATION_TABLE** v1..v7: reject(policy) → handoff(spent) →
  switch(grounding below min) → clarify(fields missing, user can supply) →
  accept → partial_answer → handoff(default).
- **FALLBACK_ROWS** (per cap): the five `CapReached` values each carry a
  still-legal action set + preferred action; `_CAP_BLOCKS` in the runtime
  maps each *blocked* action to the caps whose spending forbids it.

## Constants (config.py — all overridable in one place)

| Constant | Value | Note |
| --- | --- | --- |
| `Budget.MAX_TOTAL_LOOPS` | 6 | loop ceiling incl. re-routes |
| `Budget.MAX_TOOL_CALLS` | 6 | rag chains retrieve→answer; widened 4→6 |
| `Budget.MAX_REINTERPRETATIONS` / `_CLARIFICATION_TURNS` / `_MODEL_ESCALATIONS` / `_EXECUTION_RETRIES` | 2 / 2 / 1 / 2 | unchanged from the notes |
| `Budget.MAX_WALL_CLOCK_MS` | 30 000 | machine time only (see pause rule) |
| `Budget.LLM_CALLS_PER_DAY` | 200 | 05c quota: per-user per-UTC-day LLM-call allowance; the budget field copies it per envelope, `Store.llm_calls_today` sums the day |
| `Thresholds.GROUNDING_COVERAGE_MIN` | 0.5 | below this, v3 fires before v6 |
| `Thresholds.CONSERVATIVE_TOPK_FACTOR` | 1.5 | the entire DP-10 ladder |
| `Models.FLASH` / `Models.ESCALATED` | env-overridable renames | `ORCHESTRATOR_FLASH_MODEL` / `ORCHESTRATOR_STRONG_MODEL`; empty ⇒ llm_client default |
| `FastPath.ENABLED` | env-gated, default **off** | 05c step 3: `ORCHESTRATOR_FAST_PATH=1` lets a strong alias hit synthesize the front-half proposal with zero tokens; eligibility = deterministic half of `strong_evidence` + plain gate allow; parity-tested |
| `FastPath.TASK_TYPE` | `"faq_howto"` | the whole fast-path-eligible set, one constant — the synthesized proposal *carries* it (task type is model output on the LLM path) |

## Registry: two views, one interface

- `load_default()` — production: entries from
  `config/orchestrator/capabilities.yaml`; every key in
  `CapabilityCatalogEntry.REQUIRED_FIELDS` must be present in the *raw*
  mapping (pydantic defaults would hide a half-written entry); impls bound
  by name lazily (`rag_query`, `structured_lookup`) so `registry check`
  works with no corpus and no file db.
- `load_static()` — the test/golden fixture (echo + scripted front half).
  Every zero-LLM test and all 22 golden cases run against this view; the
  CLI `ask` tests monkeypatch `load_default → load_static` so they never
  touch real data.

Selection (`Registry.select`) iterates entries in declared order —
`structured_lookup` deliberately sits *after* `rag_query` despite both
having `["*"]`: it must only be reachable as the declared fallback, which
is what keeps e4/v3 honest paths rather than theoretical ones.
`fallback_for` skips `human_handoff` (escalation routing, not a runnable
impl) and self-references.

## The adapters, side by side

Both translate a domain result into the structured vocabulary and nothing
else; neither may decide retry, switch, or acceptance.

| Signal | rag_query (M2) | structured_lookup (M3) |
| --- | --- | --- |
| success | answered (citations included) / partial (citations *withheld* → v6 reachable) | hits found; `answer_markdown` + `records` + `result_count` |
| weak | clarify → `user_constraint_missing` (reaches e5); insufficient/escalated → `insufficient_evidence` | zero hits → `insufficient_evidence`; empty query → `user_constraint_missing` |
| failed | any exception → `dependency_unavailable` | db unreadable → `dependency_unavailable` (FTS syntax errors can no longer reach here — fixed in `storage.fts`, see M3 notes) |
| grounding | `grounding_coverage` = cited claims / claims, v3 gate | none — records are data, entry has no validation rule |
| DP-10 | `conservative` widens `retrieve(k=ceil(k×1.5))` | no-op by design (recall/precision tradeoff doesn't exist for an exact list) |

`file_manager` imports are lazy inside `run` (the web stack must not load
for `registry check`); `SearchQuery` tokens go through the shared
`storage.fts` fold/quote path, so Chinese and dotted filenames both work.

## Persistence (store.py)

Four tables via `storage.SqliteClient` (WAL, PRAGMA from the shared
layer): `requests` (envelope JSON + denormalized `status` for listing),
`runtime_objects` (`kind`, per-request `seq`, payload JSON — append-only),
`events` (the ordered narrative `replay` reconstructs), and
`handoff_tickets` (05d worklist — a pure consumer of persisted packets:
claim reads a packet to prove existence and copy its `request_id`, no
code path here writes `runtime_objects`, and no ticket row means open).
The envelope is
reloaded, never patched in place across processes; `update_request` writes
the whole blob at each transition — small rows, total consistency.
`envelope.config_hash` (05d) is set once, at the runtime's single
request-create path, from `registry.config_hash` — the sha256[:16] of
the `capabilities.yaml` bytes that produced the recorded decisions;
code fixtures and pre-05d rows carry `None`, which `replay` reports as
"unrecorded" instead of backfilling.

## Tests and golden cases

- Unit tests mirror modules (`tests/test_orchestrator/test_<module>.py`);
  the only LLM in any test is a stub client asserting the strict
  `ModelInterpretation` schema is what was requested.
- `test_execution_paths.py` is the M3 acceptance file: e4/v3 switch, e5
  survives a real fallback, e3 retry, e2 exhaustion — all through the
  *real* runtime with both real adapters behind monkeypatched seams.
- Golden format (contract CH04): one JSON object per line, `turns` script
  feeding `FakeFrontHalf`; model signals must nest inside the turn's
  `"model"` dict (a top-level `confidence` is silently a deterministic
  signal — known trap).
