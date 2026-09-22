# Orchestrator Scaling Sub-plan 05b — Trust Boundary

Status: **landed 2026-09-22 (steps 1 and 4); step 1 also live-verified** —
per-locale packs (`orchestrator/packs/` with zh verbatim + first-tranche
en; `s_unsupported_locale` clarifies with zero model calls; `orchestrate
ask --locale en` proven against the real flash model) and the adversarial
test class (`test_adversarial.py`, which surfaced and fixed two holes:
see step-4 notes). Step 5 is the gateway deployment contract, now written
into `03-usage.md`. Steps 2–3 stay gated on the service host per the
trigger below. Derived from `04-scaling.md`; one of four sub-plans
(05a–05d). Suggested execution order: **2nd**, except its step 1, which
was independent and is done.

**One sentence:** close the four trust holes the ladder exposed —
language, identity, replay visibility, adversarial input — with the
per-locale pack starting now and the external-facing trio landing
before any public exposure.

Parent breaks: `04-scaling.md` A.1 (Chinese-only safety gate), §4
(identity/tenancy fields exist, semantics do not), B.1 (adversarial
input), B.3 (no idempotency).

## Why this angle second

Two of its four holes are *correctness* holes, not scale holes: the
safety gate silently absent for every non-Chinese input, and
`handoff list` returning every user's packets. Both are cheap and worth
fixing at pilot scale. The other two (identity wiring, the external
trio) only matter the moment a service host exists — which is 05a's
step-5 output. Sequencing this angle second keeps the per-locale work
off the critical path while binding identity to the host that asserts
it.

## Trigger

- Step 1 (per-locale packs): **before any non-Chinese tenant — start
  immediately**, no scale prerequisite.
- Steps 2–3 (identity/tenancy/idempotency): at the service host (05a
  step 5).
- Step 4 (adversarial golden class): before public exposure; runnable
  from the moment step 1 lands.
- Step 5 (rate limit/moderation): contract only, enforced at the
  gateway, before public exposure.

## Design sketch

### 1. Per-locale packs (independent of everything)

DP-2's assets are Chinese-only today: all six safety rows match Chinese
patterns (`safety.py:50-103`), `normalize.ALIASES` is a Chinese dict,
the front-half `SYSTEM_PROMPT` is Chinese (`interpret.py:74`), and
`OriginalInput.locale` is carried but read by nothing (verified: zero
reads). Design:

- A **pack** = one directory per locale containing a safety table
  (same row shape as today), an alias table, and a prompt template.
  `zh` is the current content verbatim — behavior frozen by the
  existing goldens must not move.
- `safety.evaluate(text, locale)`; `normalize` and the prompt select
  by the same locale.
- **Locale comes from caller context, never from the model.** A
  model-proposed locale must not decide which safety table guards the
  input — that would let the proposal side switch off the gate it is
  supposed to pass through.
- **Missing pack is conservative, not permissive.** No pack for the
  caller's locale → one explicit `unsupported_locale` row (clarify or
  handoff). Today the same input falls through to `s0_allow` and
  *nobody gets an error* — that silent default is the bug.
- Content work: authoring the `en` pack is pattern-writing plus
  review; it is the slowest part of this sub-plan and has no code
  dependency, so it starts first.

### 2. Identity + tenancy together

Per the parent: unlike the write-path fixes, this is *not* a late-stage
local patch — a tenant column added after 150k rows means backfilling
history that was never written with tenant intent.

- **Identity context object** through `run_turn`/`resume`: `user_id`,
  `tenant_id`, `identity_tier` (the `PolicyContext` field already
  exists, default `anonymous`) supplied by the caller. The CLI keeps
  `--user` for development, marked unauthenticated.
- **Tenant column on `requests`** (05a's `ensure_columns` pattern) +
  the filter in *every* read path: `objects_of_kind`, `list_requests`,
  `replay`, `handoff list`. Today `objects_of_kind` returns every
  user's handoffs to whoever asks (`store.py:166`).
- **`resume()` checks attribution**: request's user/tenant must match
  the resumer. Needs 05a's version CAS to be race-free; together they
  close the lost-update *and* the unauthorized-resume hole.
- **Raw user text in events** (`request_captured` carries the full
  input, `runtime.py:156`): reads become tenant-scoped here; the
  redaction question (should replay show other users' text at all?) is
  handed to 05d's erasure decision, which owns the retention story.

### 3. Idempotency key

`run_turn` mints `request_id` server-side (`contracts.py:202`), so an
external retry storm creates N billable, stateful requests. Design:
client-supplied `idempotency_key` (one envelope field);
`create_request` enforces uniqueness per (tenant, key); a retry with
the same key returns the existing request instead of starting a new
one. An in-flight duplicate returns the current status rather than
blocking. This is one field, one constraint, one branch — the parent
priced it exactly that way.

### 4. Adversarial golden class

The pilot assumes an internal user; an external service assumes
attackers (`04-scaling.md` B.1). The posture holds — injection targets
the *proposal* side, and the seam already absorbs model junk
(`_coerce_bool`, `interpret.py:57`) — so the hardening is:

- a golden **test class** encoding the attacks: homoglyphs, encoding
  tricks, language switching, fragmentation across turns, and
  jailbreak-shaped proposals (a `confidence=1.0` interpretation with
  absurd entities must still walk normal safety/validation rows);
- upstream moderation + rate limiting **outside** the harness (the
  harness decides; the gateway filters);
- the `en` pack (step 1) is what makes the language-switch case
  testable.

### 5. Rate limit / moderation contract

Not orchestrator code. This sub-plan specifies the seam only: the
gateway applies authN, per-identity/IP rate limiting, and optional
content moderation *before* `run_turn`; the orchestrator receives
asserted identity (step 2) and enforces quota as a boolean signal
(05c). Documented in `03-usage.md` as the deployment contract.

## Step sequence

1. Per-locale pack loader + `zh` pack (current content) + locale
   consumed from caller context + `unsupported_locale` conservative
   row. Golden: zh behavior byte-identical (the existing suite passes
   untouched), en-pack cases, unsupported-locale case. **En pack
   authoring proceeds in parallel from day one.**
2. Identity context object + tenant column + read-path filters +
   `resume` attribution (needs 05a step 3's CAS). Golden: cross-tenant
   isolation (A cannot see B's requests/handoffs/replay).
3. Idempotency key + uniqueness + retry-returns-existing. Golden plus
   a concurrency test (simultaneous same-key submits → one request).
4. Adversarial golden class (needs step 1) wired into the release
   suite.
5. Gateway contract documented in `03-usage.md`.

## Step 1 implementation notes (landed 2026-09-22)

- **Pack shape.** `src/orchestrator/packs/`: `base.py` (the two dataclass
  shapes, a leaf so zh/en can import them without a package cycle),
  `zh.py` (all three assets verbatim), `en.py` (first tranche),
  `__init__.py` (registry, `get_pack`, `UNSUPPORTED_VERDICT`). Deviation
  from the sketch's "one directory per locale, three files": the tables
  are code-as-data with compiled regexes (DP-7), so one module per
  locale — a YAML layout would only move drift risk from behavior to
  shape without buying a loader anyone asked for.
- **The sketch's call form survives.** `safety.evaluate(text, locale)` is
  still the gate entry point; `safety.py` now owns row shape +
  `first_match` + that locale-aware `evaluate`, which reaches the
  registry through a *late* import (packs build tables from safety's
  types — the module-level cycle is broken at exactly one edge).
- `normalize(text)` without a table still means zh (its default aliases
  come from the zh pack), so pre-05b call sites behave identically;
  locale-aware callers pass `pack.aliases`.
- **Unsupported locales short-circuit before the model**, stricter than
  the sketch required: `LlmFrontHalf` sees no pack and clarifies with
  `model_name="none"` — zero tokens for unchecked input, mirroring the
  refuse/handoff posture.
- **The en pack** mirrors the zh categories in the same CH01 order, with
  IGNORECASE (capitalization is signal English has and Chinese hasn't).
  `ALIASES` is empty, so normalization degrades to NFC + whitespace
  collapse — honest, not fabricated hits. Content review continues.
- **Finding — what the goldens actually freeze:** `FakeFrontHalf` scripts
  safety decisions directly, so the golden suite never touches the pack
  tables. The zh byte-freeze therefore rests on three other proofs: a
  byte-exact prompt-assembly unit test, the untouched pass of
  `test_safety.py`/`test_normalize.py`, and 17/17 goldens (routing
  unchanged). Worth knowing before anyone proposes "covering" more
  front-half assets with goldens.
- **CLI:** `orchestrate ask --locale zh|en`; `--resume` needs no flag —
  the stored envelope carries the locale. Cosmetic known gap: the CLI's
  own hint lines stay Chinese under `--locale en`.
- **Live proof (real flash model):** `ask --locale en "How do I renew my
  Chunhui savings card"` → the model answered with an *English*
  clarification question, `[awaiting_clarification]`
  `req_01M33S6DY5WKKH80CY5E53R1TH`.
- Tests: +21 (18 `test_packs.py`, 3 front-half locale cases in
  `test_interpret.py`; `StubLLM` now also captures the system prompt).
  Suite 572 passed / 1 skipped; mypy clean.

## Step 4 implementation notes (landed 2026-09-22)

`tests/test_orchestrator/test_adversarial.py` — 10 cases. Writing it
surfaced two genuine holes, both fixed in `interpret.py` the same day:

- **Cross-turn fragmentation.** The gate only ever saw
  `original_input.text`; a clarification *answer* was normalized but
  never safety-checked — so "帮我整理一下考勤数据" + answer "顺便删除所有
  数据库记录" reached the model ungated. The gate now runs on text+answer
  joined (both the normal and unsupported-pack paths). No golden churn:
  scripted front halves bypass `interpret` entirely.
- **Constraint merge order.** `_assemble` merged gate-then-model, so a
  jailbroken proposal could ship `{"requires_confirmation": false}` and
  *revoke* a write-action confirmation the safety table had demanded.
  The merge is now gate-last — proposals still add keys, they just cannot
  overwrite what the harness decided. (This was the one place the
  "harness decides, LLM proposes" rule was inverted in code.)

What the class pins beyond the fixes:

- **harness-decides invariants**: an absurd `confidence=1.0` proposal
  keeps the gate's `safety`/`action_type`; the model cannot pick its own
  safety table (locale is envelope-side, asserted via the system prompt
  the stub received); destructive English input is refused under the en
  pack with zero token cost.
- **recorded ceilings, asserted as allows**: homoglyph ("dеlеte"),
  fullwidth ("DEＬETE"), and space-split ("删 除 所 有") all evade a
  regex gate *by design* — upstream moderation owns those (step 5).
  They are written as passing tests that fail the day someone
  strengthens the tables, so the recorded weakness can't rot silently.
- The en/zh tables genuinely don't cross-match (a zh envelope with
  English "delete all rows" allows) — that is the pack contract, not a
  bug: the caller declares the locale; unknown locales get the
  conservative clarify row.

Suite after step 4: 582 passed / 1 skipped, goldens 17/17, mypy clean.

## Verification

- Existing golden suite green with zero diffs under the zh pack — the
  "behavior did not move" proof.
- New goldens: en safety rows, unsupported locale, tenant isolation,
  idempotent retry, one adversarial bundle.
- Embedding test: async + identity + idempotency under one running
  loop.

## Non-goals

- No authentication *implementation* inside the orchestrator — it
  receives asserted identity; verifying it is the gateway's job.
- No tenant backfill story — history predating the column stays
  `default`; the parent says decide at service moment, not after.
- No change to the safety table's row shape or the safety gate's
  position before any model call (DP-2 stands).
