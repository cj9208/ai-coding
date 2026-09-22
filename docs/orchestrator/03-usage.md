# Orchestrator — Usage

**One sentence:** everything you can run is the `orchestrate` CLI — `ask`
(one turn, the only command that touches an LLM), the deterministic
inspection trio `status` / `replay` / `handoff`, the zero-LLM regression
runner `golden run`, and `registry check` — plus the library entry points
behind them.

## Prerequisites

- **LLM (only for `ask`):** the shared `llm_client` convention — repo-root
  `.env` with `LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL`. Model *roles*
  are orchestrator-only overrides: `ORCHESTRATOR_FLASH_MODEL` (front-half
  interpretation) and `ORCHESTRATOR_STRONG_MODEL` (the single escalation
  tier, DP-9); empty means "fall through to `LLM_MODEL`".
  `ORCHESTRATOR_FAST_PATH=1` (default off) enables the 05c deterministic
  fast path: a strong alias hit under a plain gate allow synthesizes the
  front-half proposal with zero model calls — parity-tested to route
  identically, so turning it on changes spend, never decisions.
- **RAG corpus** (only for asks that route to `rag_query`): a built KB at
  `data/rag/kb.db` (`rag build` from `data/rag_inbox/`). The registry
  binds the real `RagQueryCapability`; no second index, no `--data-dir`
  dance — it shares rag's default location.
- **file_manager DB** (only for the `structured_lookup` fallback path):
  `data/file_manager/file_manager.db` with metadata-searchable records.
  Missing table ⇒ the adapter returns `failed/dependency_unavailable`,
  which the tables turn into a handoff — not a crash.
- **DB:** `data/orchestrator/orchestrator.db` by default
  (REPO_ROOT-anchored via `utils.paths`; created on first use).
  `--db <path>` is a **global** flag — put it before the subcommand:
  `orchestrate --db /tmp/test.db status`.

## `orchestrate ask TEXT [--resume REQ_ID] [--user U] [--locale L]`

The M1 front half (locale-pack safety gate → alias normalize → flash
interpretation) plus the full harness loop, in one process.

- Fresh request: `orchestrate ask "春晖省钱卡每月抵扣上限是多少"`.
- Answering a pending clarification: the first run prints the follow-up
  command for you —
  `orchestrate ask --resume req_... "每月100元以内"` — TEXT is then your
  *answer*, not a new question.
- `--user` records the profile in the envelope (DP-8); it is threaded
  through every object but enforces nothing in v1.
- `--locale` (default `zh`) picks the front-half **policy pack** — safety
  table, alias table, and prompt (05b). Authored packs: `zh`, `en`
  (`src/orchestrator/packs/`). Any other locale clarifies via
  `s_unsupported_locale` with zero model calls; a resume keeps the
  locale stored on the envelope.

Output shapes:

| What you see | Meaning |
| --- | --- |
| the answer markdown, then `[completed] request_id=req_...` | answered (or handoff decided — the status tells you which) |
| `需要澄清：<question>` plus the exact `--resume` line | status is `awaiting_clarification`; the envelope is already persisted, come back any time — human answer time is *not* charged to the wall clock (`wall_clock_paused_ms`) |
| `ask failed: <exc>` on stderr, exit 1 | the front half blew up (bad/missing API key, network). The harness never half-ran: nothing was decided |

Exit codes: `0` on a completed turn (whatever the final status), `1` only
on a front-half exception.

## `orchestrate status [REQUEST_ID] [--limit N]`

- No id: the last 20 requests (`--limit` changes it) —
  `request_id  status  user_id  updated_at_ms`.
- With an id: the **full RequestEnvelope** as pretty JSON — state,
  counters, budget, resolved intent. This is DP-8 made visible: the
  envelope is the whole truth of a request, and `status` renders exactly
  what `store.get_request()` round-trips.
- Unknown id: message on stderr, exit 1.

## `orchestrate replay REQUEST_ID`

The decision-path reconstruction — the reason every table row fires with
an id. Prints, in order:

1. header: status, original input, attempt counters;
2. one line per **runtime object** (`[seq] kind: payload`), append-only
   in decision order — you can watch `interpretation → routing →
   execution → execution(fallback) → outcome`;
3. one line per **event** (timestamp, event name, payload) — the feed
   `handoff` and post-hoc audits read.

Deterministic re-read of persisted data; never touches an LLM.

## `orchestrate handoff list` / `handoff export HANDOFF_ID`

- `list`: every persisted handoff packet, newest first —
  `handoff_id  req=...  reason=...`.
- `export <handoff_id>`: the six-section CH01 markdown packet (DP-3),
  rendered by `render_handoff_markdown` — copy-pasteable into whatever
  ticket system exists later. The packet is a *persisted object*, so
  export works in a different process, days later, with no live state.

## `orchestrate handoff worklist` / `claim` / `resolve` (05d)

The worklist turns packets into owned, resolvable work without touching
them (contract record G-2 — packets are only ever read; the state
machine never sees a ticket row):

- `worklist [--all]`: every packet joined to its ticket —
  `handoff_id  status  assignee  req=...  reason=...`; "open" means no
  ticket row exists, and resolved rows hide unless `--all`.
- `claim <handoff_id> --assignee OPS [--reassign]`: take an open
  packet; claiming another operator's packet fails unless `--reassign`
  makes the takeover explicit; unknown ids fail (the packet is read to
  prove existence, and its `request_id` lands on the ticket).
- `resolve <handoff_id> --assignee OPS --note "..."`: close a *claimed*
  ticket; only the claimant may resolve, and resolving an open ticket
  is refused — an unowned resolution is the black hole with a
  timestamp, which is the whole thing this table exists to prevent.

`--assignee` is self-asserted at the CLI until 05b's identity step
lands on the service host; tenant scoping of the worklist rides the
same step (the table already carries `request_id`).

## `orchestrate golden run [--file CASES.jsonl]`

The zero-LLM regression: replays scripted cases through the real runtime
with `FakeFrontHalf` + stub capabilities, in a **throwaway temp DB** (the
`store` argument is deliberately unused), then diffs emitted decisions.

Default file: `config/orchestrator/golden_cases.jsonl` (22 cases). Each
line:

```json
{"case_id": "g01_strong_accept",
 "input": "春晖省钱卡怎么用",
 "turns": [{"task_type": "test", "top_match_score": 0.9,
            "candidate_count": 1, "model": {"confidence": 0.9}}],
 "expected": {"status": "completed", "outcome": "answered",
              "fired_row_ids": ["route_r7_proceed_strong",
                                "exec_e6_proceed_grounded",
                                "val_v5_accept"]}}
```

- `turns[]` script the front half — each entry is a proposed
  `InterpretationRecord` field set. **Trap:** scores/confidence nest
  under `"model"`, not flat — mirror what the real interpret returns.
- `resume[]` (optional) supplies the user's clarification answer, so a
  two-turn case exercises r3/r4 → r7 in one run.
- `expected.fired_row_ids` is the assertion — rows, not prose. The
  companion test (`test_golden.py`) asserts the union of all cases' rows
  covers every routing-table row, which is the invariant that keeps the
  table honest.
- Exit 1 on any diff; per-case `FAIL` lines print each diff.

`#`-prefixed lines are comments, so the file carries its own header.

## `orchestrate registry check`

Validates `config/orchestrator/capabilities.yaml` against the schema —
all 11 required fields must appear in each raw entry (pydantic defaults
would hide a half-written config; `load_default` refuses to paper over
one) — then binds implementations and prints each entry with its
`task_types` and binding status. Exit 1 prints the schema error instead
of a traceback. This is the command to run after *any* YAML edit; it
loads no corpus, no file_manager web stack.

## Library entry points

| Import | What |
| --- | --- |
| `orchestrator.runtime.Orchestrator(store, registry, front_half)` | `run_turn(text, user_id=..., locale=...)` / `resume(request_id, answer)` → `TurnResult(status, response, question, request_id)`; async twins `run_turn_async` / `resume_async` for embedding in a running event loop (05a step 5 — the sync entries fail fast inside a loop). Pass any `FrontHalf` — `LlmFrontHalf` for prod, `interpret.FakeFrontHalf` for tests |
| `orchestrator.registry.load_default()` | production view: YAML + bound impls (rag_query, structured_lookup) |
| `orchestrator.registry.load_static()` | code fixture for tests/golden — regression never needs a built corpus |
| `orchestrator.store.Store(db_path)` | three-table persistence; `get_request` / `objects` / `events` / `objects_of_kind` |
| `orchestrator.config.Budget/Thresholds` | every tunable, in one namespace each (see `02-implementation.md` constants table) |

## Deployment contract: what sits in front of `run_turn` (05b step 5)

Not orchestrator code — this is the seam the *service host* must honor
before any public exposure. Division of labor, one sentence each:

- **Gateway (outside the harness):** authentication, per-identity and
  per-IP rate limiting, and optional content moderation. These filter;
  they never decide. Homoglyph/fullwidth/space-split evasions of the
  regex gate (recorded in `test_adversarial.py`) are this layer's
  problem, not the table's.
- **Orchestrator (inside the harness):** receives *asserted* identity —
  the caller has already authenticated, the harness never verifies.
  It consumes quota as a boolean signal (05c), routes on it, and keeps
  every decision deterministic and row-auditable (DP-7). The locale
  pack is likewise caller-declared (`run_turn(locale=...)`) — the
  proposal side never selects the table that gates it.
- **Rule of thumb:** anything that *filters text or counts requests per
  principal* is gateway work; anything that *decides an outcome* is
  table work inside the harness. A gateway that starts making routing
  decisions has blurred DP-1; a harness that starts verifying tokens
  has invented a trust root it cannot audit.

## Reproducing the two live proofs

Both ran on 2026-09-22 with `LLM_API_KEY` set, rag corpus = 4 synthetic
Chinese policy docs under `data/rag_inbox` (`rag build`), and a
file_manager DB containing an uploaded `Cold_Email_Campaign_Brief.pdf`
record.

1. **M2 — cited RAG answer end to end:**
   `uv run orchestrate ask "春晖省钱卡每月抵扣上限是多少"`
   → answer with two anchored citations (doc id + page);
   `orchestrate replay <id>` shows `exec_e6_proceed_grounded` →
   `val_v5_accept`; the stored execution object carries
   `grounding_coverage 1.0`.
2. **M3 — routing switch on a real fallback:**
   `uv run orchestrate ask "冷邮件活动的简报 PDF 是谁上传的"`
   → `需要澄清：…` (r3); wait >20 s (the wall-clock pause proof), then
   `uv run orchestrate ask --resume <id> "活动物料"`
   → rag misses in ms → `exec_e4` switches to `structured_lookup` → the
   real file_manager record (uploader, project, matched filename) is
   accepted (`val_v5`). Replay shows two execution objects, `rag_query`
   then `structured_lookup`.

## Recipe: add a third capability

The M3 plug-in claim, step by step (structured_lookup is the worked
example — 1 YAML entry + 1 adapter + 1 binding, zero runtime changes):

1. Write `src/orchestrator/capabilities/<name>.py` implementing
   `Capability.run(request, context) -> CapabilityResult` — return
   `success` / `weak` (with `code`, one of
   `user_constraint_missing` / `insufficient_evidence`) /
   `failed` (with `code`, e.g. `dependency_unavailable`); raise only for
   genuinely unexpected crashes (the table routes the exception to
   `capability_crashed` → retry/handoff).
2. Add the YAML entry to `config/orchestrator/capabilities.yaml` with
   **all 11 required fields**, and reference it from an existing
   capability's `fallbacks` (or give it real `task_types` — mind the
   `select()` order).
3. Bind it in `registry.load_default()` (one line).
4. `uv run orchestrate registry check` — schema first, then binding.
5. Add golden cases whose `fired_row_ids` touch any table row only this
   capability can reach.

## Traces & debugging posture

There is no separate trace file (rag has one; the orchestrator's answer
is `replay` — the object/event feed *is* the trace, persisted in the same
DB as the state). When a request behaves surprisingly, the order is
always: `status <id>` (what did the envelope end as?) → `replay <id>`
(which rows fired, in what order?) → the payload of the object that made
the decision. All three work offline against the DB, days later.
