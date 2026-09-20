# Data Contracts & Storage

> The shared vocabulary: pydantic artifacts that pass between phases, the
> SQLite layout that makes them durable, and the LLM-output conventions.
> Code refers to this doc; this doc does not refer to code.

Part of the [Overview](./00-overview.md). Location:
`src/research_agent/contracts/` — the single source of truth; other packages
import from here, never define their own cross-phase types.

---

## 1. Artifact flow (who writes / who reads)

| Artifact | Written by | Read by |
|---|---|---|
| `ResearchBrief` | API/CLI intake | Planner, Clarifying (user_context), Report |
| `ResearchPlan` | Planner | Collector, Reflector |
| `Capture` (raw, immutable) | Collector | Normalizer, Finding-extraction, Sources section |
| `Finding` | extraction (LLM over Capture) | Reflector, Clarifying, Recommender |
| `EvidencePack` | Research (assembled) | Clarifying, Recommender |
| `Clarification` / `UserAnswers` | Clarifying / user | Recommender (profile), Orchestrator (state) |
| `PreferenceProfile` | Recommender step 1 | Recommender steps 2–4, Report |
| `RecommendationResult` | Recommender | Report renderer |

Every artifact carries `session_id`, `schema_version`, `created_at`. Findings
and Captures carry `id`s of form `F*`/`C*` because those ids are user-visible
in the report (citations) — they must be stable once written.

## 2. Core models (sketch level — full field lists live in code)

```python
class Finding(BaseModel):
    id: str
    claim: str                      # one atomic, checkable statement
    kind: Literal["fact", "review", "comparison", "price", "risk"]
    touches_candidates: list[str]   # normalized candidate names, may be []
    decision_criteria: list[str]    # e.g. ["reliability", "cost"] ← what the
                                    # recommender scores on; set at extraction
    quote: str                      # verbatim span in the Capture ← audited
    capture_id: str                 # provenance
    confidence: float               # extractor's own estimate
    support_key: str                # near-dup cluster id for independence count

class EvidenceGap(BaseModel):
    id: str
    kind: Literal["evidence", "preference"]   # THE routing field (02 §5)
    description: str
    blocked_criteria: list[str]     # decision relevance, for 03's filter
    candidate_queries: list[str]    # empty for preference gaps by construction

class Question(BaseModel):          # see 03 §3 for the why behind each field
    id: str; gap_id: str; text: str; why_asking: str
    grounded_in: list[str]          # finding ids — validator enforces ≥1
    options: list[Option]           # 2–5, each optionally cites findings
    answer_shape: Literal["single_choice", "multi_choice"]
    skippable: bool = True

class Pick(BaseModel):
    rank: int | None                # None ⇒ part of a declared tie group
    tie_group: str | None
    candidate: str
    headline_reason: str            # ≤ 2 bullets worth
    strengths: list[EvidencedPoint]; watch_outs: list[EvidencedPoint]
    constraint_fit: str             # sentence per hard constraint
    evidence: list[str]             # finding ids — non-empty enforced
    confidence: Literal["high", "medium", "low"]

class RecommendationResult(BaseModel):
    picks: list[Pick]
    rejected_notable: list[Rejected]      # "why not #6"
    sensitivity: list[SensitivityNote]    # "if <gap/answer> flipped → …"
    assumptions: list[Assumption]         # incl. from skipped questions
    overall_confidence: Literal[...]
    stop_reason: str | None               # propagated from research phase
```

## 3. LLM-output conventions

- **All calls go through the shared `llm_client` package** (`src/llm_client/`,
  env convention `LLM_API_KEY`/`LLM_BASE_URL`/`LLM_MODEL`). Phase prompts use
  `chat_json(prompt, system, schema=<contract model>)` — its strict parse +
  one built-in repair turn (feeding the pydantic errors back to the model) is
  exactly this project's policy: validation failure → one repair → hard
  phase error, never best-effort parsing of prose. Each prompt's schema is
  exactly one contract model.
- Extraction prompts receive a Capture's text and must emit
  `list[Finding]` with verbatim `quote`s; the Normalizer rejects any finding
  whose quote isn't a fuzzy match inside that capture (the anti-hallucination
  hook from 02 §3).
- Temperature: planning/reflection/scoring arguments = moderate; extraction
  and validators = 0. All prompts versioned in `contracts/prompts/` with the
  model's `schema_version` pinned in session rows for replay.

## 4. SQLite layout

One DB, `data/research_agent/agent.db` (same convention as the repo's other
two projects). Append-only semantics except the session row.

```
sessions(id, phase, stop_reason, created_at, updated_at)          # 1 row
artifacts(session_id, kind, payload JSON, schema_version, seq)    # full history,
                                                                  # replayable
captures(id, session_id, url, content_hash, fetched_at, text,
         adapter, fetch_status)          # UNIQUE(url, content_hash) ← dedup+budget
findings(id, session_id, capture_id, claim, kind, support_key, …)
         + findings_fts(claim)           # FTS5 — remember fold_cjk for CJK text
gap_events(id, session_id, kind, gap_id, status)   # open/resolved folds: this is
                                         # where "evidence gap closed by which
                                         # batch / preference gap answered where"
                                         # stays auditable
budget_counters(session_id, name, used, max)       # bumped in the same txn as artifacts
reports(session_id, path, rendered_at)
```

JSON payloads vs typed columns: artifacts live as JSON blobs for evolution
(only `schema_version` + `kind` are queried); anything the *machine* filters
on (finding kind, gap status, dedup hashes) gets real columns. When a JSON
field graduates to query use, it graduates to a column.

CJK note: `findings_fts` must reuse the `fold_cjk` trick already proven in
`file_manager` — this machine's FTS5 `unicode61` drops CJK tokens (see
project memory / `docs/file-manager.md`).

## 5. Testing surfaces (why this layout is cheap to verify)

1. **Fixture sessions**: `tests/.../fixtures/<scenario>/` = a directory of
   artifact JSONs at some phase boundary. Any subagent test = load fixture as
   `SessionView`, run, assert on returned artifact. No network, no DB.
2. **Fake adapter**: a `SourceAdapter` serving canned HTML from fixtures →
   full Research pipeline exercised offline.
3. **Replay**: `python -m research_agent replay <session_id> --from PLAN`
   rebuilds downstream phases from stored artifacts — the debugging story
   when a real recommendation looks wrong.
4. **Golden evals** (03 §6, 04 §6) run over fixtures only; CI-safe.

## 6. Suggested build order

MVP slices, each independently useful:
① contracts + orchestrator with CLI + fake adapter (machine works end to
end, dumb sources) → ② real adapters + planner/reflector prompts →
③ clarifying + HITL resume → ④ recommender + report → ⑤ FastAPI + Streamlit
front-end, local_kb adapter, re-consult loop.
