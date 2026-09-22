# Orchestrator Scaling Sub-plan 05d — Lifecycle & Governance

Status: **executing 2026-09-22** — step 1 landed: the erasure decision
is recorded in the contract ("Governance Records", record G-1:
crypto-erase, effective at the first EU/PIPL tenant with legal
sign-off); step 2 landed: the handoff worklist (`handoff_tickets` +
`worklist|claim|resolve`, contract record G-2). Derived from
`04-scaling.md`; one of four sub-plans (05a–05d). Suggested execution
order: **4th** — most of its items are decisions to be made on
schedule, not code to be written early; the capability-health signal
joined late (04 ledger, §6c).

**One sentence:** decide the three governance questions the code cannot
answer by itself (erasure, registry fields, config-as-release) and
build the one additive table — the handoff worklist — that turns
persisted packets into owned, resolvable work.

Parent breaks: `04-scaling.md` §5 (handoff black hole; the
clarification TTL half lands in 05a step 2), §6c (no circuit breaker —
adopted here after the 04 ledger found it unowned), A.2 (append-only vs
erasure law), A.6 (handoff = headcount), ladder items 6–7 (registry
governance, config-as-release).

## Why this angle last

Half of it is decision work with deadlines rather than engineering with
dependencies: erasure must be decided before the first EU tenant,
registry governance when the catalog passes ~20 entries, config release
with the service host. The one buildable item — the handoff worklist —
is additive and safe at any point, but its assignee/claim semantics
only matter once handoffs/day exceed what a chat channel can absorb
(pilot ≈ 10/day is borderline; rung A is 2–4k). Sequencing it fourth
keeps the early runway clear for the data plane and the trust boundary,
where mistakes become irreversible faster.

## Trigger

- Erasure decision: **before the first EU tenant** (legal input
  required; the code posture below is ready either way).
- Handoff worklist: when handoffs/day > ~10 sustained, or at the
  service host — whichever comes first.
- Registry governance: when catalog entries pass ~20 (today: 2 — far).
- Config-as-release: with the rung-A service host.

## Design sketch

### 1. Erasure strategy — decision record first

GDPR/PIPL "right to be forgotten" vs the replay guarantee
(`01-design-rationale.md`) and append-only objects: user text lives in
`envelope_json`, every `runtime_objects.payload_json`, and `events`
payloads, and the architecture currently picks neither guarantee. The
two candidate resolutions, per the parent:

- **Crypto-erase:** per-user (or per-tenant) data keys encrypt the
  text fields; deleting the key renders every copy unrecoverable with
  zero row edits — the cleanest fit for append-only, at the cost of
  key-management infrastructure.
- **Tombstone projections:** blank the text fields in place, preserve
  row-ids and audit structure; cheaper to run, but it *edits* history,
  which the replay story must then define precisely.

Lean: crypto-erase, because it leaves the append-only store and the
row-id audit untouched (DP-7's compliance value at rung B is exactly
"these rows fired, in this order"). Either way the deliverable is a
recorded decision in the contract — **before the first EU tenant, not
after the first complaint** — and until then the contract's "Explicitly
Not Doing" carries this item explicitly.

**Landed as contract record G-1 (2026-09-22):** the lean became the
decision — crypto-erase, per-user data keys at the store's
serialization boundary, key deletion with zero row edits, envelope
contract unchanged; tombstone rejected because it edits history and
would make the DP-7 audit artifact conditional on the erasure ledger.
The record fixes the shape, not the key-management product; effective
date and legal-sign-off posture are as sketched above, and "Explicitly
Not Doing" now points at G-1 instead of carrying the question silently.

### 2. Handoff worklist (the one additive build)

DP-3 froze handoff as packet + markdown export — the right v1 call,
and at rung A's 2–4k packets/day an unclaimed black hole. The design
keeps that freeze intact:

- new table `handoff_tickets`: `handoff_id` (references the persisted
  packet), `request_id`, `status` (open/claimed/resolved),
  `assignee`, claimed/resolved timestamps, resolution note. **A
  consumer of persisted packets** — packets themselves do not change,
  DP-3 untouched;
- CLI: `handoff claim|resolve|worklist` alongside `list|export`;
- worklist rows never enter the state machine — the deliberate absence
  of the `handoff → routing` edge (`runtime.py:63`) stands. The human's
  resolution returning to the machine is a **contract conversation
  (DP-3 amendment)** tracked here, not coded around;
- multi-tenancy: assignee scope comes from 05b's identity context.

**Landed 2026-09-22 (contract record G-2; deltas from the sketch
above):**

- "open" is implemented as **the absence of a ticket row**, not a
  status value: creating a packet can never race a claim, and packets
  persisted before the worklist existed appear correctly without any
  backfill. `status` on the row is therefore derived
  (claimed/resolved), and the worklist output is a join of stored
  packets onto ticket rows.
- `claim` reads the packet only to prove existence and derive its
  `request_id` — there is no write path to `runtime_objects` in this
  table, so DP-3's freeze is structural, not a convention. Pinned by
  `test_worklist_churn_leaves_the_packet_byte_identical` (payload
  after the full claim/reassign/resolve cycle equals the payload at
  creation); the packet-immutability check is a pytest, not a golden,
  because goldens exercise routing rows and the worklist has none.
- takeover is explicit (`--reassign`), and resolve requires the
  claimant — the claim→resolve order is what stops the worklist from
  becoming a timestamped black hole.
- No state-machine touch, assertable: `runtime.py` is outside the diff
  (`LEGAL_TRANSITIONS` unchanged); worklist rows never enter the loop.

### 3. Registry governance — contract conversation

Four catalog fields exist *for* governance — `rollout_status`,
`cost_profile`, `latency_profile`, `use_when`/`avoid_when` — and no
runtime decision reads them (verified write-only). At 50–200
capabilities, first-match-in-declaration-order selection (DP-5) becomes
a collision hazard. The decision, exactly as the parent frames it:
either selection grows to consume them (a **DP-5 amendment** — the same
rigor as the original decision, since single-level selection is a
deliberate contract position) or the fields are deleted from the
schema. This sub-plan carries the decision record and the deadline
(entries > ~20); the code follows the decision, never precedes it.

### 4. Config-as-release

Rung A's data-residency regimes make configuration tenant data:
`capabilities.yaml`, thresholds, alias packs, and budgets become
per-region artifacts that must be versioned and released, not files an
operator edits under a running service. Two concrete pieces land here:

- **the golden suite as the release gate it already is** — behavior
  changes to config are code-equivalent and ride the same suite; this
  is a process commitment, written into `03-usage.md`;
- **`envelope.config_hash`** — the hash of the config artifact that
  processed the request, recorded on the envelope. Replay then
  reproduces *which configuration* produced the recorded decisions,
  closing the silent-drift hole where a request from config-v3 is
  replayed against a v5 world. One field, one write site (the request
  create path), replay display only.

### 5. Capability health signal (adopted from the 04 ledger, §6c)

A dead downstream has no trip state today: every request rediscovers
it via full timeout, then walks `switch_capability` per-request. The
parent's shape holds — a `health: degraded` boolean the routing and
validation tables can read, DP-9-style (a boolean state feeding
existing rows, not new control logic). What the ledger adds is where
it lives: the bit is *runtime registry state*, not durable request
state, so it must **not** ride the envelope (DP-8 has nothing to say
about a read-only hint) and the trip/reset policy is an in-process
window (N consecutive `capability_crashed`/timeout results), which
makes it unobservable to replay — acceptable only because routing
decisions that *consume* the bit log their row id (DP-7). Trigger:
the first capability with real external dependencies behind the
service host (05a step 5), i.e. precisely when a standing process can
hammer a dead peer.

## Step sequence

1. ✅ Erasure decision record in the contract (2026-09-22, record G-1:
   crypto-erase chosen over tombstone, effective date = first EU/PIPL
   tenant with legal sign-off; implementation stays gated there).
2. ✅ `handoff_tickets` + `claim|resolve|worklist` CLI + immutability
   tests (2026-09-22, record G-2; the packet-unchanged assertion is a
   pytest, not a golden — see §2 notes).
3. `envelope.config_hash` + replay display + release-checklist section
   in `03-usage.md`.
4. Registry-governance decision record (drafted when entries > ~20;
   the two options above are the menu).
5. Health signal: trip window + routing/validation rows consuming
   `health` (a DP-5 row addition — contract note in the design doc
   first, same rigor as any table change); lands with the service
   host, never before.

## Verification

- ✅ Goldens → replaced by tests: worklist claim/resolve lifecycle
  (store + CLI), unknown-packet refusal, reassign/claimant rules, and
  packet immutability under worklist churn.
- ✅ Suite green; no state-machine transition changes (asserted: the
  worklist diff touches `store.py`/`cli.py` only; `LEGAL_TRANSITIONS`
  in `runtime.py` is untouched).

## Non-goals

- No external ticket-system integration — the worklist is the v1;
  wiring to a real ticketing system is an adapter decision later.
- No `handoff → routing` recovery edge without a DP-3 contract
  amendment.
- No multi-region deployment — only the release-artifact mechanism
  that would make it possible.
- No retroactive erasure implementation until decision 1 lands.
