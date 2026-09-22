# Orchestrator Scaling Sub-plan 05b — Trust Boundary

Status: **proposed 2026-09-22** — derived from `04-scaling.md`; one of
four sub-plans (05a–05d). Suggested execution order: **2nd**, except
its step 1, which is independent and can start today.

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
