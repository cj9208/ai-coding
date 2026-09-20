# Recommendation Agent — Design

> Fuses the two halves of the system — what the world says (EvidencePack) and
> what the user said (answers + assumptions) — into a short ranked list where
> every entry carries its reasoning, its evidence, and its failure conditions.

Part of the [Overview](./00-overview.md). Runs at RECOMMEND, after DEEPEN.

---

## 1. Pipeline

```
EvidencePack + UserAnswers + assumptions[]
      │
      ▼
┌────────────────────┐   explicit weights, each tied to a gap_id/answer
│ 1. Preference      │   or a stated default; "taste" criteria allowed
│    Profile         │   with low weight rather than dropped
└──────┬─────────────┘
       ▼
┌────────────────────┐   union of candidates mentioned across findings,
│ 2. Candidate Set   │   normalized (aliases, versions), trimmed by hard
│    construction    │   constraints from answers (budget → drop, not score)
└──────┬─────────────┘
       ▼
┌────────────────────┐   per candidate × criterion: evidence-backed rating
│ 3. Scoring         │   + a second pass that argues the *other* side
└──────┬─────────────┘
       ▼
┌────────────────────┐   ranked 3–5 + "why not #6" + sensitivity notes
│ 4. Rank & Report   │   + confidence, assumptions restated
└────────────────────┘
```

## 2. Preference Profile — the merge point

The user's answers don't get "kept in mind"; they compile into a typed
artifact so ranking is reproducible and auditable:

```jsonc
{
  "hard_constraints": [                       // from answers: filter, don't score
    {"key": "budget_cny", "value": "<=2000", "from": "ques_1/answer"}
  ],
  "criteria": [                               // scored
    {"key": "reliability", "weight": 0.35,
     "from": "ques_2 → prod_side_project", "direction": "higher_is_better"},
    {"key": "ecosystem",  "weight": 0.25, "from": "default (user skipped)"},
    {"key": "learning_curve", "weight": 0.2, "from": "free-text answer quote"},
    {"key": "cost",       "weight": 0.2, "from": "hard constraint softened: value matters"}
  ],
  "taste_notes": ["prefers boring technology", "hates vendor lock-in"]  // LLM-readable context for step 3
}
```

Rules: weights sum to 1; every `from` is traceable to an answer, an answer
quote, or a declared default (which feeds the assumptions list). The model
proposes the profile; code validates (sums, unknown gap refs, constraint
conflicts — e.g. budget too tight for any surviving candidate → surface the
conflict to the user instead of silently relaxing it).

## 3. Scoring — argument-based, not number-first

The failure mode of "rate each candidate 1–10 per criterion" is confident
numbers over thin evidence. Instead the primary pass is **argument
construction**: for each (candidate, criterion), the LLM must produce the
strongest *citation-backed* case for and against, drawn only from findings
whose `decision_criteria` touch that criterion; a rating is emitted **after**
and **from** those arguments, with `abstain` legal when no finding covers it.

Second mechanism: **adversarial re-rank**. One pass asks "make the best case
that the current #2 beats #1" over the same evidence. If that case is as
strong, the two are presented as a *tied headline* ("pick A if X, B if Y —
here's the knife-edge") rather than a false ordering. Ranking pretension is
the most common dishonesty in rec systems; the design admits ties.

Abstinence rule: if fewer than two candidates survive hard constraints with
any evidence, the honest output is "the research doesn't support a confident
pick; here's what's missing and what we'd ask you next" — returned as a
result, not an error.

## 4. Report format (the deliverable)

One markdown file per run (`reports/{session}.md`), stable skeleton:

```
# Recommendation: <topic>            ← 3-line TL;DR: top pick + one reason + confidence
## Your situation                     ← restated profile + assumptions, user-checkable in 20s
## Ranked picks
  #1 <name>  ★★★★☆                   ← why #1 (2 bullets max)
     strengths / watch-outs / fit-with-your-constraints / evidence: F3,F7,F12 / confidence
## The near-tie (if any)              ← the knife-edge framing from §3
## Also considered, and why not       ← incl. "why not #6" — kills the black-box objection
## What would change this ranking     ← sensitivity: "answer Q2 differently → B wins"
## Sources                             ← captures, grouped, with fetch dates
```

The sensitivity section is not decoration: it is the fallback path for a user
who skipped questions, and it doubles as the re-consult hook ("I actually
care more about privacy" → flip criteria, re-score, **no re-research** when
findings already cover it).

## 5. Confidence model

Aggregate of: coverage of decision-criteria by evidence (Reflector's
ratings), source independence of top picks (corroboration count from dedup),
stop_reason ≠ null (budget-cut research caps confidence), and skipped-question
density. Surfaced once in the TL;DR per pick, not as per-sentence hedging.

## 6. Evaluation

- **Golden sessions:** fixture EvidencePacks + known-answer profiles →
  expected ranking (human-labeled, small set). Metric: top-1 and NDCG@3
  agreement.
- **Citation audit (automatic):** every rating traceable to ≥1 finding; every
  finding's quote verifiable in its capture (round trip to 02's guarantee).
- **Persona sweep** (shared with Clarifying's §6): same EvidencePack ×
  conflicting personas — ranking must move in the direction the answers
  predict; rankings that don't react to explicit preferences are broken here.

Next: [05-data-contracts-and-storage](./05-data-contracts-and-storage.md).
