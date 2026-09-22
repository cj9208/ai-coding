# Orchestrator — Design Rationale

This document argues *why the harness is shaped this way*. The decision
record — DP-1..DP-10, the milestone order, the per-milestone implementation
notes — lives in [`../orchestrator-design.md`](../orchestrator-design.md)
and wins on wording; this is the narrative version of it, plus the reasoning
that only became visible after shipping. Reference stack: the blog note set
`AI_study/rag-orchestration-architecture` (CH00–CH04); we adopt its layer
model and deviate where the contract says so.

## Big picture

```text
                    ┌──────────────────────────────────────────────────────┐
   user text ──────►│  FRONT HALF  (the only probabilistic side)           │
                    │  safety gate ─► normalize ─► flash model (chat_json) │
                    │   deterministic  deterministic   proposes ONLY       │
                    └───────────────┬──────────────────────────────────────┘
                                    │ FrontHalfOutput (typed signals)
                    ┌───────────────▼──────────────────────────────────────┐
                    │  HARNESS  (deterministic, this package's center)     │
                    │                                                      │
                    │  routing table ──► decide ─► budget legality ─► act  │
                    │       ▲                                   │          │
                    │       │        ┌──── retry/switch/clarify ┤          │
                    │       │        ▼                          ▼          │
                    │  validation ◄── capability ◄── execution table       │
                    │  table           run           (e1..e7)              │
                    │  (v1..v7)                                             │
                    │                                                      │
                    │  every state change: _transition (sole writer),      │
                    │  every decision: row id recorded, everything         │
                    │  persisted on the envelope + runtime_objects         │
                    └──────────────────────────────────────────────────────┘
```

The whole design reduces to one division of labor: **the model proposes,
the harness decides.** Nothing else in the package is novel — the value is
that the division is enforced structurally, not by prompting discipline.

## The spine is the seven runtime objects

```text
RequestEnvelope ──► InterpretationRecord ──► RoutingDecision
  the only durable     what the front half       which table row fired,
  state (DP-8)         proposed, typed           what is still legal
        │                                            │
        ├──► CapabilityExecutionRecord ◄─────────────┘
        │       what the capability reported
        ├──► FinalOutcome                 object 5: answer / partial /
        │                                    handoff, with validation summary
        ├──► HandoffPacket                object 6: the six CH01 sections,
        │                                    complete enough to take over
        └───► CapabilityCatalogEntry     object 7: config-time, who may run
```

Three properties of this spine do the load-bearing work:

- **Every object records the *row id* that produced it**
  (`table_row_id`, `decision_reason.primary`). "Why did it hand off?" is a
  query, not an archaeology session — and it makes the coverage claim below
  mechanically checkable.
- **Signals, not scores.** The objects carry booleans and enums the tables
  read (`confidence_state`, `user_resolvable_ambiguity`,
  `grounding_coverage`), never a blended "confidence number" the harness
  would have to reverse-engineer. DP-9's "state, not formula" is what keeps
  the tables readable as *policy*.
- **Capabilities report, never decide.** `CapabilityResult` has status +
  structured code (`weak/insufficient_evidence`,
  `failed/dependency_unavailable`) — a capability cannot ask for its own
  retry, and a crash is mapped by the runtime into `capability_crashed`
  rather than propagating. The clean-failure rule (CH02_02) survives
  integration precisely because the vocabulary is this small.

## Tables as data, with row ids

Each decision table is an ordered list of `Row(predicate, action, reason)`
— first match wins, catch-all last (DP-7). The point of the row-id scheme
is the test invariant: **every row must be fired by at least one golden
case**, checked mechanically. A decision table you cannot enumerate is a
decision table you cannot audit; a table whose rows no test exercises is
dead policy that reads like live policy.

Row *order* is load-bearing policy, not style: hard constraints first
(policy block, budget exhausted), clarify before expensive reasoning
(a wrong-but-confident answer is more expensive than one question),
terminal default last so "fell through" is always visible as
`*_default`.

## Why budgets live on the envelope

DP-8 (envelope as sole durable state) is the least obvious and most
consequential choice: counters, budgets and status are one JSON blob
persisted at every transition, so a CLI process that dies mid-request
leaves *nothing* inconsistent — `ask --resume` reconstructs spent budget
from the row, not from memory.

Two consequences shipped code has to honor (both fixed at M3, see contract
notes):

- **The wall clock budgets machine work, not human latency.** A request
  parked in `awaiting_clarification` accrues paused time that is subtracted
  at the resume edge; the 30 s ceiling still kills a stalled *loop*.
  Counters (`clarification_turns`) stay charged — the human gets unlimited
  thinking time, but only two clarifications.
- **Fallback legality is per branch, not global.** A spent cap forbids only
  the action that would consume it (`_CAP_BLOCKS`); the tempting "union of
  all forbidden actions" veto once implemented silently made an exhausted
  retry budget forbid clarifying. The current shape reads each CH02_02
  "still legal" list literally, and the fallback walk provably terminates
  because handoff/reject/accept are never blocked.

## Why the registry, when there are two capabilities

The registry (YAML entries, 11 required fields enforced at load, impls
bound by name) exists for one claim: **adding a capability changes zero
runtime code.** M3 tested that claim honestly — `structured_lookup` arrived
as one YAML entry + one 153-line adapter + one `load_default` binding, and
what made it observable was not new machinery but *old* machinery finally
reachable: because rag now has a runnable declared fallback, table rows
e4/v3 (`switch_capability`) went from "correct but never fired" to the
actual routing path of a live request.

Deliberate simplifications where the blog is richer (each recorded as a DP
in the contract — do not "complete" them from the notes):

| DP | Simplification | Why it's fine |
| --- | --- | --- |
| DP-5 | Selection is one level: `task_type → entry`; no domain routing step | one domain per capability at this scale; `domain_scope` is carried as ownership metadata only |
| DP-6 | Capabilities get an observational plan field, no graph planner | the loop already bounds work; planning graphs need a failure mode to defend against |
| DP-10 | Conservative mode = one adapter line (`k = ceil(k × 1.5)`) | the notes' escalation *ladder* needs several capabilities; widening the first retrieval is the part that does something today |

## Why Chinese input is a deterministic citizen

The front half's *interpretation* is a model call, but everything that can
be deterministic is: the safety gate is a pattern table evaluated before
any token is spent (a refusal costs zero), and `normalize` is an alias
table with a specificity score, not a fuzzy matcher. Chinese short names
("报销" for 费用报销系统) resolve through declared aliases with traceable
rule hits — the model never sees raw ambiguity that a five-line table could
have resolved, and every normalization is replayable. On the retrieval
side this inherits `storage.fts`'s `fold_cjk` contract rather than
re-solving CJK tokenization (decision #2 in the user-locked list).

## Handoff: prepared, not integrated

DP-3 keeps escalation honest and small: the handoff packet is the six CH01
sections as a persisted object + markdown export. No ticket system is
wired — "核心是先准备好". The object is the contract a future integration
must satisfy; building the integration first would freeze the shape before
three milestones of real decisions had flowed through it.

## What was explicitly *not* argued away

Permission enforcement (`policy_context` is threaded everywhere and
enforced nowhere), multi-tenant auth, the HTTP service form factor,
confidence calibration beyond the tables — all listed under "Explicitly Not
Doing" in the contract, with the seams documented so each can be added
behind an existing object. The honest framing: this is a *decision*
harness ready for consequences, not yet a security boundary.
