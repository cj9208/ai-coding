# Orchestrator — Design

> Owns the session lifecycle: which phase runs next, what the budgets are,
> and how the process survives the human-in-the-loop pause. It contains **no
> domain intelligence** — if a decision requires knowing anything about the
> topic, it belongs to a subagent, not here.

Part of the [Overview](./00-overview.md). Contract: the orchestrator is the
only module allowed to advance `session.phase` and to hand artifacts between
subagents.

---

## 1. Why a separate orchestrator at all

Two reasons, both practical:

1. **The HITL pause breaks the "one process" assumption.** Between CLARIFY
   and DEEPEN, the user may answer in 10 seconds or 3 days. Something has to
   own *where we left off and with what accumulated context*. That is a state
   machine, not an LLM call.
2. **Termination is a policy, not a model judgment.** "Stop researching after
   N calls / when saturated / when the user says stop" must be enforced
   deterministically. If we let the research model decide when it's done,
   cost is unbounded.

## 2. State machine

```
            ┌──────────────────────────────────────────────┐
            │                                              │
INTAKE ──► PLAN ──► COLLECT ──► REFLECT ──► CLARIFY ──► AWAIT_USER
                        ▲            │                      │
                        │            │ (keep researching)   │ answers / skip
                        └────────────┘                      ▼
                                                       DEEPEN ──► RECOMMEND ──► DONE
                                                          │           │
                                             (answers opened       (hard failure:
                              no new angles)  no new angles)        degrade+notify)
                                                          └────► RECOMMEND ◄─┘
```

Transition table (the only place transitions are written):

| From | Condition | To | Executor |
|---|---|---|---|
| INTAKE | brief validated | PLAN | — (pure validation) |
| PLAN | research plan exists | COLLECT | Research |
| COLLECT | batch of findings stored | REFLECT | Research |
| REFLECT | `continue_researching and budget_ok` | COLLECT | Research |
| REFLECT | saturated/budget-exceeded | CLARIFY | Clarifying |
| CLARIFY | questions non-empty | AWAIT_USER | Clarifying |
| CLARIFY | `nothing_to_ask` | DEEPEN or RECOMMEND | — |
| AWAIT_USER | answers received (or skip) | DEEPEN | — |
| DEEPEN | follow-up done or empty | RECOMMEND | Research |
| RECOMMEND | report written | DONE | Recommendation |
| any | budget violated / user abort | FAILED (state kept) | — |

Every transition is a single transaction: write the new phase **and** the
artifact it produced, then commit. Crash-safety is "re-read the session and
continue" — there is no in-memory state to lose.

## 3. Budgets

The orchestrator holds the counters; subagents receive remaining budget as an
input parameter so they can plan within it (e.g. the planner sizes the number
of sub-queries to the remaining collector calls).

```python
@dataclass(frozen=True)
class Budget:
    max_collect_calls: int = 30      # network/LLM calls by collectors
    max_research_iters: int = 3      # COLLECT→REFLECT loops (phase 1)
    max_deepen_calls: int = 8        # second, targeted collection
    max_questions: int = 4           # per clarification round
    max_clarify_rounds: int = 2      # v1 default: 1; 2nd only if answers spawn a real gap
    wallclock_seconds: int = 600
```

On violation the orchestrator does **not** discard work: it forces the
machine forward to the next terminal-ish phase with whatever evidence exists
and marks `stop_reason="budget"` so the recommendation lowers confidence
accordingly. Failing loudly with no output is worse than a hedged answer.

## 4. Resumption and `AWAIT_USER`

`AWAIT_USER` is the only phase where the process may exit normally. The
session row stores: phase, all artifacts so far, and the rendered questions.
Resumption = `POST /sessions/{id}/answers` loads the row, appends the answers
as a `UserAnswers` artifact, and re-enters the machine at DEEPEN.

This is also why the subagents must be **pure functions of their input
artifacts** — no hidden client state — because a resumed run reconstructs
everything from the DB.

## 5. Routing decisions that could have gone either way

- **Skip clarification entirely?** Yes, allowed. If the Clarifying Agent
  returns `nothing_to_ask` (query is self-contained, e.g. "recommend a Rust
  HTTP framework for a solo side project" with everything stated), we jump to
  RECOMMEND. The user is never forced through a ceremony.
- **Who decides a second clarification round?** The Clarifying Agent proposes
  (its gap analysis runs again over DEEPEN findings), the orchestrator checks
  `max_clarify_rounds`. Proposal-by-model, policy-by-code.
- **Where does "user just chats instead of answering" go?** Answers are
  free-text per question plus an optional global comment. A non-answer is
  treated as a skip for that question; Clarifying never re-asks the same
  question (dedupe by `gap_id`).

## 6. Interface each subagent must satisfy

```python
class Phase(Protocol):
    name: str
    def run(self, session: SessionView, budget: BudgetView) -> PhaseResult: ...

# PhaseResult = ok(next_hint) | blocked(reason) | done(report_ref)
```

`SessionView` is a read-only snapshot of artifacts; `BudgetView` exposes
remaining counters. Subagents return artifacts; they never write the DB and
never call each other. That protocol is what makes each of docs 02–04
independently testable with fixture sessions (see §5 of
[05-data-contracts](./05-data-contracts-and-storage.md)).

## 7. Package layout (repo integration)

```
src/research_agent/
  orchestrator/     # state machine, budgets, session store access
  research/         # planner.py, collector.py (adapters/), reflector.py
  clarifying/       # gap selection, question builder, validator
  recommendation/   # profile builder, scorer, report renderer
  contracts/        # pydantic models shared by all (single source of truth)
  api/              # FastAPI app (v1)
  cli.py            # MVP driver
tests/research_agent/
```

Next: [02-research-agent](./02-research-agent.md) — how the EvidencePack in
the middle of this machine actually gets built.
