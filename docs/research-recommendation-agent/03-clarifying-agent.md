# Clarifying Agent — Design

> Converts the Research Agent's *preference gaps* into the smallest set of
> questions whose answers would most change the recommendation — each one
> grounded in a cited finding, so the user can feel the agent did its
> homework before opening its mouth.

Part of the [Overview](./00-overview.md). Consumes `EvidencePack`, produces
`Clarification` (questions) or `nothing_to_ask`.

---

## 1. The core question this agent answers

Per candidate gap, exactly one test:

> **Would different plausible answers to this gap produce a different
> ranking?**

If yes → candidate question. If no → silently fold into "default assumption",
which the report will state anyway (§ of 04 doc). This *decision-relevance*
filter is the entire job; everything else is formatting and cost control.

## 2. Pipeline

```
 preference_gaps[]                 (from Research, deduped by gap_id)
      │
      ▼
┌───────────────┐  drop gaps failing decision-relevance (LLM + rules)
│ 1. Filter     │  drop gaps the user already answered (scan user_context,
└──────┬────────┘   earlier rounds' answers) — dedup by gap_id, never re-ask
       ▼
┌───────────────┐  score = rank_impact × uncertainty × answer_cost⁻¹
│ 2. Rank &     │  hard cap: max_questions (orchestrator budget)
│    select     │  round-2 rule: propose only, orchestrator may refuse
└──────┬────────┘
       ▼
┌───────────────┐  each selected gap → Question with options derived FROM
│ 3. Ground &   │  findings (real candidates, real price bands, real
│    build      │  trade-off pairs) — not generic "low/med/high"
└──────┬────────┘
       ▼
┌───────────────┐  validator (pure code, no LLM):
│ 4. Validate   │  · every question references ≥1 finding_id  ✔ else dropped
│               │  · every option cites a source or is marked "user's own"
│               │  · 2–5 options + always an implicit "other/skip"
│               │  · no evidence_gap leaked in               ✔ else dropped
└───────┬───────┘
        ▼
   Clarification  →  orchestrator renders, sets AWAIT_USER
```

## 3. What a question looks like

```jsonc
{
  "id": "ques_2",
  "gap_id": "pg_7",
  "text": "Is this for personal skill-building, or will you deploy it where
           downtime has a cost?",
  "why_asking": "F12 and F15 both flag B's managed tier as the reliability
                 differentiator; A is cheaper only if that doesn't matter.",
  "grounded_in": ["F12", "F15"],
  "options": [
    {"value": "personal_learning", "label": "个人学习/玩具项目"},
    {"value": "prod_side_project", "label": "会长期跑的小产品", "cites": ["F12"]},
    {"value": "client_facing",     "label": "客户可见，稳定性优先"}
  ],
  "answer_shape": "single_choice",     // + free-text box always available
  "skippable": true
}
```

Design points:

- **`why_asking` is mandatory.** It is the one surface where the user *sees*
  the research; it also lets the model-user simulation in evals (§6) check
  grounding mechanically: `why_asking` must name the criterion the options
  differ on.
- **Options come from the evidence.** "Real candidates, real bands" — the
  LLM must construct options out of findings (A vs B, the price clusters that
  appeared, the two reliability postures sources disagreed on). Free-text is
  always accepted alongside, since multiple-choice can be wrong about the
  user even when right about the world.
- **Prefer fewer, coarser.** Two good questions beat five mediocre ones. The
  ranking formula divides by answer-cost precisely to bias toward questions
  that are *cheap to answer and expensive to get wrong* (a 2-second tap that
  reorders the whole list).

## 4. Ordering and framing rules

Questions are ordered by **re-inspection value**: an answer that could make
later questions unnecessary goes first (if "personal learning only" is
answered, the compliance question may evaporate — round 2 exists for what
survives). The set is presented as *one message* with context ("based on what
I found, two things would sharpen the recommendation"), never a drip of
separate turns — interrogation fatigue is a budget problem, and the budget
cap is per *round*, not per question.

Bilingual note: question text follows the user's language (this repo's owner
works in Chinese + English); `value` keys stay English-stable for scoring.

## 5. Nothing-to-ask and skip handling

- **`nothing_to_ask`** is a first-class, common output — short-circuits to
  RECOMMEND. Trigger: the brief already fixes the decision-relevant variables
  (rare for taste-heavy topics, frequent for technical ones). Returning
  questions when none are decision-relevant is a *failure*, and the eval
  suite measures the false-positive rate of this.
- **User skips** a question → the folded default assumption is used and
  *recorded* in `assumptions[]` with the gap_id, so the recommendation's
  sensitivity notes can say "if you actually need X, flip #1 and #3".
- **User answers off-axis** ("well it depends…") → treated as free-text
  evidence about preferences; the Recommendation profile builder reads answer
  text, not just option values.

## 6. Evaluation (how we know this agent is any good)

Three checks, all offline against fixture sessions:

1. **Grounding audit** (automatic): every surviving question has matching
   `grounded_in` findings; option strings appear in or paraphrase findings.
2. **Necessity test** (LLM-as-judge): given the full EvidencePack, would a
   strong recommender's ranking change under different answers to this
   question? Questions judged non-decisive were wrongly selected.
3. **Simulated users**: persona fixtures (budget-constrained, expert,
   ambivalent) answer our questions; we score final-recommendation match
   against the persona's known ideal list, with and without the clarification
   phase. The value of clarification must show up as a number.

Next: [04-recommendation-agent](./04-recommendation-agent.md).
