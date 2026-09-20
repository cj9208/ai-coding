# Research Agent — Design (Planner · Collector · Reflector)

> Turns a free-form user interest into an **EvidencePack**: findings with
> source citations, plus two lists that the rest of the system consumes —
> *evidence gaps* (what's still unknown about the world) and *preference
> gaps* (what no search can tell us about the user).

Part of the [Overview](./00-overview.md). Runs in two modes sharing one code
path: **Phase-1 broad research** (PLAN→COLLECT⇄REFLECT) and **Phase-3
DEEPEN** (one targeted pass driven by the user's answers).

---

## 1. Internal structure

```
 ResearchBrief (from INTAKE)
      │
      ▼
┌──────────┐    sub-queries with intent tags,    ┌────────────┐
│ Planner  │ ── priority, adapter hint ────────► │ Collector  │
└──────────┘                                     │ (parallel) │
      ▲                                          └─────┬──────┘
      │ revise plan                                    │ raw captures
┌───────────┐                                           ▼
│ Reflector │ ◄────────────────────────────  ┌──────────────────┐
│ gap check │                                │ Normalizer +     │
│ saturation│                                │ Deduper (per     │  SQLite `captures`
└───────────┘                                │ capture)         │  (raw text stays)
      │
      ▼
 EvidencePack { findings[], evidence_gaps[], preference_gaps[], stop_reason }
```

Three internal roles, deliberately separate models-of-work:

| Role | Kind | Why separate |
|---|---|---|
| Planner | LLM, structured output | Query decomposition is the highest-leverage prompt in the whole system; it deserves its own eval |
| Collector | code + adapters, LLM only for extraction | Deterministic network behavior; retries live here |
| Reflector | LLM over compact digest, not raw text | Deciding "am I done" needs a *summary view*; feeding it 40 full pages wastes the very budget it guards |

## 2. Planner

Input: `ResearchBrief{topic, user_context, candidate_domain_hint}` + budget.
Output: `ResearchPlan` — a list of sub-queries, each with:

```jsonc
{
  "id": "q3",
  "query": "X vs Y comparison 2026 site:reddit.com OR forum",
  "intent": "comparison",        // survey | comparison | constraint | recency | risk
  "adapter_hint": "web_search",  // soft hint, Collector may downgrade
  "priority": 1,                 // 1=must-run, 2=run-if-budget, 3=only-if-gaps
  "expected_findings": "how practitioners weigh X vs Y on reliability"
}
```

Design points:

- **Intent typing forces diversity.** A plan of six "survey" queries is the
  classic failure (everything says the same thing). The schema requires each
  query to carry one of the fixed intents, and the prompt instruction is to
  cover ≥3 intents; the validator rejects single-intent plans for topics that
  have recommendation-shaped answers.
- **`preference_gaps` start here.** The planner also emits *hypothesized*
  unknowns about the user ("depends on whether offline use matters"). These
  flow to the Reflector and then to the Clarifying Agent — the questions we
  ask the user are seeded at planning time, then filtered by what research
  actually left open.
- **Plans are re-plannable.** In DEEPEN mode the Planner receives prior
  findings' gap list + user answers and produces a plan restricted to
  `priority=1` follow-ups. No second planner implementation.

## 3. Collector

A thin dispatcher over **source adapters**, all with one interface:

```python
class SourceAdapter(Protocol):
    kind: str                    # "web_search" | "page_fetch" | "local_kb" | ...
    def search(self, q: str, k: int) -> list[Hit]: ...
    def fetch(self, hit: Hit) -> Capture: ...   # raw text + provenance
```

v1 adapters:

| Adapter | Backed by | Notes |
|---|---|---|
| `web_search` | search API via `httpx` | returns ranked hits, no full text |
| `page_fetch` | `httpx` + `markdownify` | body extraction; strips boilerplate |
| `local_kb` | `ai_market_radar` SQLite | free, offline, already fingerprinted — reuse for AI-topic research |
| `user_docs` | `file_manager` metadata search | "research *my* notes/emails too" — later milestone |

Pipeline per sub-query: `search → top-k fetch → store Capture (raw, immutable)
→ extract Findings (LLM, JSON mode)`. Two storage layers on purpose:

- **Capture**: exactly what the page said, with URL, fetch time, content hash.
  Findings can be re-extracted with a better prompt without re-fetching —
  this keeps extraction iteration cheap and keeps the audit chain honest.
- **Finding**: one atomic claim, typed, with `quote` (verbatim span from the
  Capture) + `source_id` + `confidence`. The quote requirement is what makes
  hallucinated "evidence" detectable: a finding whose quote can't be matched
  in its capture is flagged automatically by the Normalizer.

**Dedup** runs at both layers: URL/content-hash for Captures (also protects
the budget), near-duplicate claim detection for Findings (so three blogs
copy-pasting one press release don't count as three corroborations — the
Reflector sees `support_count` distinct sources instead).

**Failure policy:** adapter error → retry ×2 → record in
`source_failures[]`; the Reflector treats a failed high-priority query as a
gap and may re-plan it with a different adapter hint. The Research Agent
never hard-fails the session for a dead URL.

## 4. Reflector (loop control)

Runs after each COLLECT batch on a **digest view** (finding titles + intents
covered + gap lists + counters — a few hundred tokens, not the pages).

Outputs `ReflectDecision`:

```jsonc
{ "coverage": {"survey": "good", "constraint": "thin", "risk": "missing"},
  "evidence_gaps": [...], "preference_gaps": [...],
  "continue_researching": true,
  "next_queries_hint": ["..."],        // fed back to Planner, not executed directly
  "saturated": false, "reason": "constraint-intent findings contradict on price; need recency" }
```

Stop conditions (any one ends Phase-1):

1. **Saturation** — a new batch adds no finding that changes any coverage
   rating or gap. Cheap heuristic first (novelty of claim keys), LLM judgment
   second; we stop on *two consecutive* low-novelty batches.
2. **Budget** — orchestrator counters (see [01 §3](./01-orchestrator.md)).
3. **Coverage floor met** — every intent in the plan rated ≥ adequate.

Why saturation-based and not fixed-iteration: fixed "research 5 times"
over-collects trivial topics and under-collects hard ones; the loop count is
the knob, novelty is the signal.

## 5. The gap taxonomy (contract with the Clarifying Agent)

The single most important output distinction of this subagent:

- **evidence gap** — answerable by more research. *Never* asked to the user.
  Blocks RECOMMEND only if it touches a decision criterion (else it just
  lowers confidence).
- **preference gap** — answerable only by the user (constraints, taste,
  context). Every one becomes a *candidate* question; the Clarifying Agent
  selects among them.

Mis-routing here is the root of both classic failure modes: asking the user
what Google could tell us, or recommending while ignorant of budget. The
schema makes the distinction a required field, and the two docs downstream
treat the lists as disjoint namespaces.

Next: [03-clarifying-agent](./03-clarifying-agent.md).
