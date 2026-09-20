# Research & Recommendation Agent — Overview Design

> **One-sentence core:** given any topic the user is interested in, the agent
> researches it, asks the user *informed* questions based on what it found,
> then delivers a ranked recommendation where every claim traces back to
> collected evidence.
>
> This is the entry-point document for the whole design. Each subagent has its
> own deep-dive doc (index below). Read this one first; the others assume you
> know the phase model in §3.

---

## 1. Why this shape

The naive version — "one big prompt: research X and recommend something" —
fails in three predictable ways:

1. **It asks nothing about the user.** Recommendations are preference-shaped.
   Without knowing constraints (budget, skill level, use context), the output
   is a generic listicle.
2. **It asks too late, or pointlessly.** If we ask questions *before*
   researching, we burden the user with questions we could have answered
   ourselves. The whole point of this design: **collect first, then ask only
   what the evidence cannot answer**.
3. **It cannot be debugged or improved.** One mega-agent mixes search
   breadth, preference inference, and ranking logic; when the output is bad,
   you can't tell which stage broke.

So the split is by **failure mode**, not by fashion:

| Subagent | Owns | Fails as |
|---|---|---|
| **Orchestrator** | state machine, budgets, HITL resumption | stuck loops, lost sessions |
| **Research Agent** (planner → collector → reflector) | gathering enough trustworthy evidence | shallow/one-sided coverage |
| **Clarifying Agent** | turning *evidence gaps about the user* into minimal, grounded questions | bad questions, interrogation fatigue |
| **Recommendation Agent** | criteria weighting, ranking, justification | unranked dumps, unsourced claims |

## 2. Big picture

```
 user query / topic / interest
        │
        ▼
┌────────────────┐
│  ORCHESTRATOR  │  owns session state, budgets, resumption
└───────┬────────┘
        │ Phase 1: PLAN
        ▼
┌─────────────────────────────────────┐
│  RESEARCH AGENT                     │
│  ┌─────────┐  ┌──────────┐          │
│  │ Planner │→ │Collector │          │   adapters: web search, page fetch,
│  └─────────┘  └────▲─────┘          │   local KB (ai_market_radar), docs
│        ┌───────────┴─────┐          │
│        │ Reflector       │ loop ≤N  │   coverage + saturation check
│        └─────────────────┘          │
└───────────────┬─────────────────────┘
                │ EvidencePack (findings + gaps)
                ▼
┌─────────────────────────────┐
│  CLARIFYING AGENT           │  picks ONLY decision-relevant open questions;
│  2–4 grounded MC questions  │  may return "nothing to ask" → skip Phase 3
└───────────────┬─────────────┘
                │ user answers  (human-in-the-loop pause)
                ▼
┌─────────────────────────────┐
│  RESEARCH AGENT (second run)│  targeted follow-up: candidates/angles that
│                             │  the answers just made relevant
└───────────────┬─────────────┘
                ▼
┌─────────────────────────────┐
│  RECOMMENDATION AGENT       │  build preference profile → score → rank
│  ranked recs + evidence +   │  with citations, trade-offs, confidence
│  sensitivity notes          │
└───────────────┬─────────────┘
                ▼
        report (md) + session stored for feedback / re-run
```

Note the loop is **not** research → ask → recommend in one pass: after the
user answers, we do one *targeted* second collection (cheap, bounded) so the
recommendation can cite fresh facts the answers implied — e.g. the user says
"budget under ¥2000" and we then look up actual prices instead of guessing.

## 3. Phase model (the contract all docs share)

```
INTAKE → PLAN → COLLECT ⇄ REFLECT → CLARIFY → [await user] → DEEPEN → RECOMMEND → DONE
                                      │                                  ▲
                                      └── (no questions) ────────────────┘
```

Every phase reads and writes typed artifacts on the session (see
[05-data-contracts](./05-data-contracts-and-storage.md)); no subagent talks
to another subagent directly. This is what makes each piece testable with a
fixture session.

## 4. Design principles

- **Evidence before empathy.** Never ask the user something a search could
  answer. Every clarification question must reference at least one finding ID
  that motivated it — enforced in the Clarifying Agent's validation step.
- **Bounded by budget, by default.** Max collector calls, max research
  iterations, max 4 questions, 2 rounds of clarification. "Enough" is a
  first-class output (the `stop_reason` field), not an accident.
- **HITL is resumable state, not a live call.** The session persists at
  `AWAIT_USER`; the process may die between asking and answering. This drives
  the SQLite-first storage choice.
- **Traceable or it didn't happen.** Every recommendation cites finding IDs;
  every finding cites a source URL/document. The report is re-readable
  without re-running anything.
- **Graceful degradation.** If the user skips questions, recommend with
  declared assumptions. If a source fails, record it in `source_failures` and
  lower confidence rather than silently dropping evidence.

## 5. Tech stack (reuse what's in this repo)

| Concern | Choice | Already used by |
|---|---|---|
| LLM calls | shared **`llm_client`** package (`src/llm_client/`): env convention + `chat()`/`chat_json()` with retries | `pdf_summarizer` (via adapter) |
| Storage | SQLite + SQLAlchemy | `file_manager`, `ai_market_radar` |
| HTTP/ingest | `httpx`, `feedparser`, `markdownify` | `ai_market_radar` |
| API surface | FastAPI (later: Streamlit chat UI) | `file_manager` |
| Models | Pydantic v2 for all phase artifacts | `llm_client`, repo-wide |

Planned location: `src/research_agent/` with one module per subagent
(§7 of orchestrator doc for the package layout).

## 6. Docs map — what each file is for

| Doc | Read it when |
|---|---|
| [01-orchestrator](./01-orchestrator.md) | You want to know who drives the state machine, how budgets and resumption work |
| [02-research-agent](./02-research-agent.md) | You care about how information is actually collected (planner/collector/reflector internals) |
| [03-clarifying-agent](./03-clarifying-agent.md) | You want the question-generation logic: what makes a question worth asking |
| [04-recommendation-agent](./04-recommendation-agent.md) | You want the ranking/justification method and report format |
| [05-data-contracts-and-storage](./05-data-contracts-and-storage.md) | You're writing code — schemas, DB layout, package skeleton |
| [06-usage-guide](./06-usage-guide.md) | You want to *run* the MVP: CLI walkthrough, budget levers, where data lives, extension points |

## 7. Roadmap

- **MVP (proves the loop):** ✅ implemented & live-verified 2026-09-20 — see
  [06-usage-guide](./06-usage-guide.md). CLI-driven; sources = web search +
  page fetch
  only; fixed single recommendation domain passed by the user
  (e.g. "laptops", "courses", "libraries"); 1 clarification round.
- **v1:** FastAPI session endpoints + markdown report; local-KB source
  adapter (reuse `ai_market_radar` data); sensitivity analysis in reports;
  re-consult ("I care more about X") without full re-run.
- **Explicitly out of scope for v1:** multi-user auth, fine-tuned models,
  automated source discovery, real-time data (prices that move hourly).
