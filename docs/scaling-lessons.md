# Scaling Lessons — What Two Systems Taught Us to Ask at Design Time

**One sentence:** every scaling problem we actually hit in `rag` and
`orchestrator` (plus the shared-layer bugs they exposed) collapses to a
handful of questions that cost minutes to ask at design time and a
migration to skip.

**Who reads this:** anyone starting a new subsystem — before writing
the schema. Each lesson links to its full story; this file is the
digest, not the evidence. The evidence lives in `docs/rag/04-scaling.md`
and `docs/orchestrator/04-scaling.md` (whose Problem status ledger is
the model for keeping such a digest *current*).

```text
        design time                bench time              run time
 ┌───────────────────────┐   ┌───────────────────────┐   ┌──────────────────┐
 │ §1 version: list or   │   │ §2 what does the bench │   │ §4 which config  │
 │    copy? §3 does law  │──▶│    HIDE? per-stage,    │──▶│    produced this │
 │    collide with retain?│   │    fake the slow thing│   │    record? §5    │
 │ §5 who owns write==   │   │                       │   │    shared-layer  │
 │    query knowledge?    │   │                       │   │    single owner  │
 └───────────────────────┘   └───────────────────────┘   └──────────────────┘
                 §6 everything not yet built is a parked decision
                    with a named trigger — the ledger is the state
```

## 1. Decide what a "version" is: a manifest, or a copy

**What we hit (rag):** `chunks(chunk_id, corpus_version)` — every
publish copied every chunk's text (~5 GB per version at 100k docs),
and publish was one wipe-and-reindex transaction taking tens of
minutes, with no resume point. The daily 1k-doc increment cost as much
as the historical backfill. The redesign hinge (`04-scaling.md`) is one
sentence: *version lists chunk ids, it does not own rows* — immutable
content-addressed chunks + a `snapshot_chunks` manifest; rollback
becomes pointing at the old manifest, and a full refresh is the
degenerate case of the incremental path, not a separate command.

**What we hit (orchestrator):** the opposite trap — append-only
`runtime_objects` with no versioning at all would silently allow two
processes to race the same envelope. Fixed with a CAS token
(`requests.version`, typed `ConflictError`): single-writer *per
version*, cheap because versions are list-like, not copies.

**Design-time questions:** Does your version column duplicate data, or
enumerate it? Where is the GC for unreachable history? Who is allowed
to write the same row twice, and what happens on the second write?

## 2. A bench that includes the slow thing hides the fast thing's contention

**What we hit (orchestrator):** the concurrency bench must fake the
LLM — not for cost, because real 1–10 s model latency naturally
serializes workers and *masks* SQLite write contention. With fakes, the
truth showed up in a specific order: P99 ballooned 32 ms → 1016 ms →
2515 ms across 1w→4w→8w writers with **zero** `database is locked`
errors; the first lock errors appeared only at 16 writers (0.39 %).
A clean, error-free concurrency run is not evidence the write path is
safe — tail latency is the canary. Fixing the right thing followed from
per-stage numbers: batching took 45 → 137 turns/s; two missing indexes
took `list_requests` P50 from 188 ms to < 1 ms at 100k rows.

**What we hit (rag):** the 250k-chunk latency table attributes 99.6 %
of query P50 to `fts_sql` (BM25) and everything else to sub-millisecond
— which is what licenses "no query-side rewrite" as a conclusion
instead of a guess, and prices the M2 vector decision at its real
threshold (~250k chunks, not "someday").

**Design-time questions:** Which component, if left real, would hide
the failure you are looking for? Does the bench report per-stage
breakdowns, or only end-to-end numbers you can't act on?

## 3. Append-only storage and erasure law collide on the design, not the first complaint

**What we hit (orchestrator):** user text lives in the envelope, in
every `runtime_objects.payload_json`, and in `events` — the replay
guarantee and GDPR/PIPL erasure were both promised and the architecture
picked neither. Contract record G-1 decides it before any EU tenant:
crypto-erase (per-user data keys at the serialization boundary, key
deletion = zero row edits, the row-id audit survives untouched);
tombstones were rejected because editing history makes the audit
artifact conditional on the erasure ledger.

**Design-time question:** name the legal regime that could demand
deleting bytes you swore to keep forever, decide the mechanism, record
it with an effective date — the decision is the deliverable, the code
can wait for the trigger.

## 4. Config is behavior; every persisted record must say which world produced it

**What we hit (orchestrator):** requests persisted months ago replay
against today's `capabilities.yaml` — silent drift. Record G-3 shipped
the closure early (the hole exists on one machine): `envelope.config_hash`
is set at the single create path, `replay` prints match/DRIFT/unrecorded,
and the golden suite is the *written* release gate for config changes.
Pre-05d rows show an honest `None`; nothing was backfilled.

**What we hit (rag):** the same lesson inverted — the *identity* of the
config that produced a chunk is baked into `chunk_id` (pipeline
fingerprint), which is what makes incremental publish correct at all.
And the deliberate carve-out matters as much: enrich prompts and
embedding models must **not** enter that fingerprint — they are side
projections keyed `(chunk_id, provider_version)`, so iterating a prompt
never re-chunks the library.

**Design-time questions:** If an operator edits config at 2 a.m., what
must tomorrow's audit be able to prove about requests made before the
edit? Which config belongs in the data's identity, and which must be
kept out of it?

## 5. In the shared layer, one bug is N bugs — put write-end and query-end knowledge under one owner

**What we hit (storage, found via rag + file_manager + orchestrator):**
SQLite FTS5's default tokenizer drops CJK, so text must be *folded
identically on both ends* — when the fold lived per-project, drift was
one refactor away; `fold_cjk`/`match_expr`/`FtsTable` now live in
`src/storage` precisely so the write and query ends cannot diverge.
The punctuation follow-up: a folded unit like `a.pdf，` emitted bare
into a MATCH query raises an FTS5 *syntax error* that every caller
misread as "dependency unavailable" — fixed once in `token_expr`,
rescuing three consumers. Same posture for WAL/`busy_timeout` PRAGMAs:
generic access knowledge goes in the shared layer; table definitions
and business semantics stay in the project.

**Design-time question:** is this fact *how to touch the DB* (shared,
one owner for both ends) or *what the data means* (project-local)? A
wrong answer multiplies every bug by the consumer count.

## 6. Parking is shipping: a gated decision with a named trigger beats the feature

**What we hit (both):** the honest outcomes were mostly *not* code.
Postgres for the orchestrator store: closed as "won't trigger" with
reopen conditions. The answer cache: NO-GO until real traffic prices
its hit rate — the cost model was written first, and the fast path it
did green-light shipped behind a default-off flag with a parity test.
The capability health signal: "lands with the service host, never
before". Registry governance: gated at ≈20 entries, count checked
(3 today). And where a gate belongs in the system, it goes through the
contract: the per-user quota is routing row r10 with a contract
amendment, not an `if` bolted around the tables.

The meta-device is the **Problem status ledger** (04-scaling): one
table of fixed / measured / open / decided-unbuilt, each row with owner
and trigger. It caught an unowned problem (§6c) and routed it into a
sub-plan — which is the ledger doing its only job: making "we have not
built it" a *decision with a date*, not amnesia.

**Design-time questions:** For each anticipated scale problem: what is
the trigger that makes building it correct? Can you name it now? Then
write the record and build the check — not the feature.

## The design-day checklist

Copy these into the next subsystem's design doc and answer them there:

1. Does a "version" copy rows or list ids? Where does dead history get GC'd?
2. Who may write the same record twice — and what detects it?
3. Which slow component must the bench fake, so the fast contention stays visible?
4. Does the bench report per-stage costs, and what is the *measured* threshold for the next storage decision?
5. Which legal regime can collide with the retention promise — mechanism + effective date, decided now?
6. What identity goes into the data's key, what stays a side projection, and does every record name the config that produced it?
7. Is each DB fact access knowledge (shared layer, one owner for write+query) or business meaning (project-local)?
8. For every "not yet": what is the trigger, who owns it, and where is the ledger row?
