# Orchestrator Scaling Sub-plan 05a — Data Plane

Status: **steps 1–5 landed and measured 2026-09-22 — the data plane is
complete** — derived from `04-scaling.md`; one of four sub-plans
(05a–05d). Suggested execution order: **1st**. The write path is
benchmarked before and after, the batching + CAS work shipped, **step 5
landed the async surface (the harness is now embeddable in an ASGI
host) and the capability handle lifetime**, and **step 6 (Postgres) is
closed as "won't trigger"** by the step-4 evidence (see step 4's
verdict). Only the gated step 6 remains, and its trigger never fired.

**One sentence:** make the harness embeddable in an async service and
make its SQLite writes batched and collision-safe — proving both with a
stubbed bench *before* touching any storage engine.

## Baseline (2026-09-22, current code)

Bench: `scripts/orch_bench_run.py` — zero-LLM (FakeFrontHalf + golden
registry), N worker processes driving the golden suite as legal
state-machine walks against one fresh shared SQLite db per point.
Raw numbers: `data/orchestrator_bench/results_{sweep,seed}.json`.

Metric 1+2 — concurrency sweep (`--workers 1 4 8 16 --turns 400`,
~400 turns/worker, three runs; numbers stable across runs):

| workers | turns | locked | P50 | P95 | P99 | mean | txns/turn |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | 400 | 0 | 16 ms | 32 ms | 47 ms | 21 ms | 19.8 |
| 4 | 1,600 | 0 | 31 ms | 297 ms | 1,078 ms | 82 ms | 19.9 |
| 8 | 3,200 | 0 | 31 ms | 875 ms | 2,203 ms | 156 ms | 20.2 |
| 16 | 6,400 | 25 (0.39%) | 32 ms | 1,828 ms | 3,907 ms | 306 ms | 20.2 |

Reading:

- §2's ~15–25 txns/turn estimate confirmed at ~20 (mix ≈ 1 create +
  6 update + 5 objects + 8 events per turn) — the batching target is
  real: ~20 commits where 2–4 would do.
- Zero `database is locked` through 8 writers: pysqlite's default 5 s
  busy-timeout converts contention into *tail latency* long before it
  errors (P99 32 ms → 2.2 s from 1 → 8 workers). At 16 writers the
  timeout is finally reached — 25/6,400 hard failures. This is §2's
  "no busy_timeout" made visible: silent latency corruption first,
  errors second.
- Anomaly (open): 1 of 3 runs at 16 workers threw `AssertionError`
  (runtime.py loop invariants) on 2/6,400 turns; identical seed did not
  reproduce in runs 2–3, so it is timing-dependent, not script-dependent.
  Root cause unknown; carried into step 3 — if CAS + batched txn does
  not eliminate it, it gets a dedicated reproduction test there.

Metric 3 — ops surface (`--seed-requests 100000`, 136 MB db, 5k
handoff objects among 105k object rows):

| call | P50 | P95 |
| --- | --- | --- |
| `list_requests(limit=20)` | **188 ms** | — |
| `objects_of_kind("handoff")` | ~0 ms | 16 ms |

- §6's index claim confirmed for `list_requests`: unindexed
  `ORDER BY updated_at_ms DESC` sorts 100k rows *per call*. The
  `runtime_objects(kind, created_at_ms)` index is preventive (the scan
  is still cheap at 10^5 objects) but justified by the same trajectory.

Parent breaks: `04-scaling.md` §1 (deployment shape), §2 (write path),
§6 (unindexed reads, stale capability handles).

```text
 today ──► 05a data plane ──► host exists ──► 05b identity lands on it
              │                                 │
              │ bench curve                     └─ per-locale pack: parallel lane,
              ▼                                    starts today (05b step 1)
        Postgres sibling ── only if the curve
        says SQLite's ceiling is below target   05c quota step: no dependency on 05a
```

## Why this angle first

The parent's own words: the process-model decision is "the only real
decision", and until it is taken "the rest of this document is a
shopping list with a blocked checkout". At both ladder rungs the
decision is already forced ("async service"), so there is nothing to
wait for — and everything else (identity, idempotency, the answer
cache) assumes a long-lived process.

## Trigger

Now. The pilot rung owes the §1 decision anyway, and §2's fixes are
required by *both* §1 outcomes (batching details differ; the version
CAS is needed regardless). The Postgres phase (step 6) is the only
gated part: it starts only if the step-1/4 bench shows SQLite's writer
ceiling below pilot-A traffic.

## Design sketch

### A. Bench harness first (`scripts/orch_bench_run.py`)

Mirror of `scripts/rag_bench_*` for this axis. The golden machinery
already stubs the LLM (`FakeFrontHalf`, `ScriptedCapability`,
`golden.registry_for`) — the bench reuses it: N worker processes hammer
one `orchestrator.db` with legal state-machine walks (ask → clarify →
resume → answered/handoff mix driven by scripted results).

Three measurements, matching the parent's Verification debt:

1. write contention: `database is locked` rate and P99 per turn vs.
   worker count (turns §2's estimates into a curve);
2. per-turn transaction count × duration, before and after step 3
   (the batching win, quantified);
3. `status` / `handoff list` latency after 10^5 seeded requests (turns
   §6's index claim into numbers).

### B. Write discipline (§2)

- **`requests.version` compare-and-set.** Add an integer `version`
  column (the shared layer already has `ensure_columns`,
  `src/storage/sqlite.py`); `update_request` becomes
  `UPDATE … WHERE request_id=:rid AND version=:v` with `version = v+1`,
  raising a typed `ConflictError` on rowcount 0. Two processes resuming
  the same `awaiting_clarification` request stop silently losing each
  other: the loser gets an error it can turn into a retry or a
  `handoff`. Envelope rewrites flow through the transition path, so the
  CAS has one choke point to instrument.
- **One transaction per loop pass.** Today `store.py` opens a
  transaction per method (~15–25 per turn, `04-scaling.md` §2). Add a
  unit-of-work context (`Store.transact()`) and let `runtime._drive`
  commit transition + objects + events of one pass together. Target
  ~2–4 transactions per turn.
- **Seq onto the envelope.** `append_object` currently does
  `SELECT MAX(seq)+1` then insert (`store.py:131`) — a read-then-write
  race across kinds. The envelope already carries per-request state
  (DP-8); add `next_seq` to it and pass the seq in. The
  `UNIQUE(request_id, kind, seq)` constraint stays as the backstop.
- **Defense in depth in the shared layer.** `src/storage/sqlite.py:53`
  sets only WAL + foreign_keys; add `busy_timeout` there — generic
  engine-level policy, not orchestrator business logic.

### C. Cheap storage wins (§6)

Two `CREATE INDEX` statements, justified by bench metric 3: one on
`runtime_objects(kind, created_at_ms)` for `objects_of_kind`
(`store.py:166`), one on `requests(updated_at_ms)` for
`list_requests` (`store.py:111`).

Plus clarification TTL: one `awaiting_since_ms`-style field on the
envelope and one check in `resume()` — resume after N days forces
re-interpret against the current registry/alias/corpus view instead of
silently continuing a stale interpretation (`04-scaling.md` §5).

### D. Async surface (§1) — control loop untouched

- `interpret.py:117` (`asyncio.run(self.client.chat_json(...))`) and
  `capabilities/rag.py:56` (`asyncio.run(generate(...))`) both raise
  `RuntimeError` under a running event loop — this is the only thing
  preventing ASGI embedding. Make `FrontHalf.interpret` and
  `Capability.run` **coroutines**; inside, wrap the existing *sync*
  `llm_client` calls in `asyncio.to_thread`. The shared LLM layer
  (`src/llm_client/`) does not change — the repo rule stands.
- `_drive` stays a synchronous while-loop; only its entry points get
  async wrappers, and a sync shim (`asyncio.run` at the boundary) keeps
  the CLI and the entire zero-LLM golden suite running unchanged.
  *(Landed narrower than written: the loop's decision structure is
  unchanged, but `_drive` itself had to become a coroutine to await the
  two slow points — see step 5's deviation note.)*
- Regression test that pins the win: interpret + one capability run
  inside a *running* event loop must not raise.

### E. Capability handle lifetime (§6, the standing bug)

`RagQueryCapability._get_store` caches `RagStore` forever
(`capabilities/rag.py:68`) — harmless when process lifetime ≈ one
question, a standing bug in a long-lived one. Fix: resolve the handle
per call against the current `corpus_version` (reopen on change, cache
otherwise). 05c's answer cache keys on the same version, so this fix
is its prerequisite — it lands here, where service-ification makes it
necessary.

### F. Postgres sibling — gated, not scheduled

`src/storage` anticipates a `postgres.py` sibling, but per the parent:
batching *must* land first (20 round-trips/turn × 100 QPS is a
connection-pool story, not an engine story). Step 6 starts only if the
bench curve says SQLite's ceiling is below target; when it does, the
work is: an env-driven engine switch in `orchestrator.config`,
`storage/postgres.py` behind the same engine/session seam, and
dual-run verification (golden suite + replay diff on both engines).

## Step sequence

1. ✅ `scripts/orch_bench_run.py` + baseline run against **current** code
   (metrics 1–3 recorded above; run 2026-09-22).
2. ✅ **Landed 2026-09-22** — `busy_timeout` + two indexes + clarification
   TTL:
   - `storage/sqlite.py` gains `BUSY_TIMEOUT_MS = 5000` applied as a PRAGMA
     per connection (it names what pysqlite already did implicitly, and the
     constant records the measured finding: waiting converts contention into
     tail latency, it does not remove it);
   - `orchestrator/store.py` `_SCHEMA` gains `ix_requests_updated`
     (`updated_at_ms`) and `ix_objects_kind` (`kind, created_at_ms`);
     `CREATE INDEX IF NOT EXISTS` means existing dbs adopt them at next open;
   - `orchestrator/config.py` gains `Lifecycle.CLARIFICATION_TTL_MS` (7
     days) and `runtime.resume()` uses the already-persisted
     `state.wait_started_ms` to detect a stale wait: past the TTL the
     machine-side attempt counters and capability selection are cleared
     (fresh re-interpret), `clarification_turns` survives as the lifecycle
     bound, and a `clarification_expired` event keeps the gap auditable
     (DP-7). No new envelope field was needed.
   - Tests: 3 added (PRAGMA applied, indexes present, TTL restart), full
     `tests/test_orchestrator` + `tests/test_storage` green (173), golden
     suite 17/17.
3. ✅ **Landed 2026-09-22** — seq-on-envelope + version CAS + batched pass
   transaction:
   - **CAS**: `requests.version` (added to `_SCHEMA` and backfilled
     additively through `SqliteClient.ensure_columns`, so pre-existing dbs
     upgrade on open); mirrored onto the envelope as `state.version` with
     the **column as authority on read**; `update_request` is now
     `UPDATE … WHERE request_id=:rid AND version=:old` and raises the typed
     `store.ConflictError` on rowcount 0. `_transition` remains the only
     caller, so the CAS has one choke point.
     *Judgment call*: `ConflictError` propagates rather than becoming a
     decision-table row — inventing a conflict row would amend DP-5/DP-7
     without a contract conversation. The caller decides: the CLI prints it,
     a queue worker would retry.
   - **Seq**: `state.next_seq` on the envelope (DP-8), assigned by
     `runtime._append`; `Store.append_object` takes an optional `seq` and
     only falls back to `MAX(seq)+1` for callers outside a turn. The
     `UNIQUE(request_id, kind, seq)` constraint stays as the backstop.
   - **Batching**: `Store.transact()` groups one loop pass's writes into one
     commit; every writer goes through `_write()` which joins the open
     connection, and **reads join it too** (`_read()`) because a pass reads
     back objects it appended (the handoff packet does) before they are
     committed.
     *Judgment call*: the two slow points stay **outside** any transaction —
     `front_half.interpret` and `impl.run(ctx)` — so batching never holds
     the SQLite write lock across an LLM or a rag query; those stages wrap
     only their own writes.
   - Tests: 5 added (CAS conflict incl. loser-copy-stays-honest, commit and
     rollback as a unit, read-your-own-writes inside a pass, envelope-derived
     seq, resume-elsewhere refusal); `tests/test_orchestrator` +
     `tests/test_storage` at 178 green, golden suite untouched.
4. ✅ **Measured 2026-09-22** — bench re-run against steps 2–3
   (`results_after_{sweep,seed}.json`, same seed, same 400 turns/worker):

   | workers | locked before → after | P99 before → after | mean before → after | throughput before → after |
   | --- | --- | --- | --- | --- |
   | 1 | 0 → 0 | 47 → 31 ms | 21 → 10 ms | 41 → 77 turns/s |
   | 4 | 0 → 0 | 1,078 → 750 ms | 82 → 41 ms | 45 → 85 turns/s |
   | 8 | 0 → 1 | 2,203 → 1,313 ms | 156 → 52 ms | 46 → 123 turns/s |
   | 16 | 25 → 11 | 3,907 → 2,407 ms | 306 → 89 ms | 44 → 137 turns/s |

   - **The headline is the throughput curve, not the error count.** Before,
     throughput was *flat* at ~45 turns/s no matter how many writers were
     added — 20 commits/turn × 45 ≈ 900 commits/s is the serialized
     single-writer wall, so extra workers bought nothing. After: 8 commits
     per turn (from ~20 writes; 2.5× fewer lock acquisitions) and
     throughput that now actually scales — 137 turns/s at 16 workers, 3.1×
     the baseline plateau.
   - P50 is 15–16 ms at *every* worker count (was 32 ms at 8–16), and the
     single-worker case improved too (mean 21 → 10 ms): fewer fsync
     round-trips helps even with no contention.
   - Lock errors did not vanish — 11/6,400 at 16 writers (0.17%, was
     0.39%) — because contention is reduced, not eliminated; pysqlite still
     waits out `BUSY_TIMEOUT_MS` before erroring.
   - The step-1 `AssertionError` anomaly (2/6,400) did **not** recur in any
     post-step-3 run, consistent with its root cause being the uncommitted-
     mid-pass divergence that per-pass batching removes. Left as: no
     dedicated reproduction test was ever needed.
   - Metric 3 after the indexes: `list_requests(20)` P50 **188 ms → <1 ms**
     at 100k requests; `objects_of_kind("handoff")` P95 16 ms → <1 ms.

   **Step 6 verdict — closed as "won't trigger" for pilot-A write
   throughput.** 2–6M txns/day (A.5) is ~70 commits/s on average against
   the ~1,100 commits/s the post-step-3 path sustained — two orders of
   magnitude, and SQLite capacity was never the near wall (per-request data
   is tens of KB). The honest qualifier: at 100 QPS sustained the harness
   would sit at ~73% of a ceiling measured with *no* real capability or LLM
   work in the loop, so the binding constraints are elsewhere — the
   deployment shape (step 5) and the LLM quota (05c), not the storage
   engine. Step 6 reopens only if: commits/turn regresses upward, one file
   must serve multiple regions, or the read path (not measured here)
   becomes the constraint.
5. ✅ **Landed 2026-09-22** — async surface (D) + capability handle
   lifetime (E) + embedding regression test:
   - `FrontHalf.interpret` and `Capability.run` are **coroutines**
     (protocol changed; the docstring says why). `LlmFrontHalf` now
     `await`s `llm_client.chat_json` directly (the `asyncio.run` at
     `interpret.py:117` is gone — the repo rule "LLM calls go through
     llm_client" is untouched, its client was always async); the rag
     adapter awaits `generate` and runs sync `retrieve` through
     `asyncio.to_thread`; the lookup adapter thread-hops its sync
     search. Fakes became trivially async.
   - *Deviation from sketch D, stated plainly*: D said "`_drive` stays
     a synchronous while-loop" — impossible once its two slow points
     are awaited. What the sketch cared about (the loop's *structure*)
     is intact: same states, same passes, same commits; `_drive`
     itself is now a coroutine, and the sync `run_turn`/`resume` are
     boundary shims that `asyncio.run` the async entries — guarded by
     `_refuse_inside_loop`, so misuse inside a running loop fails with
     a message naming the async entry instead of leaking asyncio's.
   - `RagQueryCapability._get_store` reopens when the kb file's
     `(mtime_ns, size)` changes — a `rag build` that swaps the file no
     longer leaves a long-lived host reading the old inode; an
     in-process publish mutates the same file and stays visible
     through the cached handle's fresh read transactions. 05c's answer
     cache can now key on this store's `active_version`.
   - Tests: `test_async_surface.py` (7) pins the win from the host's
     seat — everything awaited runs on pytest-asyncio's live loop, so
     a re-introduced nested `asyncio.run` fails loudly; the earlier
     capability/front-half tests became async. Orchestrator+storage at
     185, repo-wide 551 green, golden suite still 17/17 through the
     sync shims, and a 4×100 bench smoke through the new shim showed
     the same 7.9 commits/turn and zero lock errors (no write-path
     regression from the loop hop).
   - Live proof (the posture agreed at step 1: real calls only after
     the async conversion): one `orchestrate ask "春晖省钱卡每月抵扣
     上限是多少"` — real flash interpret (awaited), r7 proceed, rag
     retrieve+generate (awaited) → completed with anchored citations,
     `req_01M33R05…`. The two awaited slow points work against a real
     provider, not just fakes.
6. Postgres sibling — only on a bench-demonstrated trigger. Step 4's
   verdict says it never fires at pilot-A throughput.

Each step is independently committable; 2 and 3 are valuable even if
the pilot stays CLI-shaped forever.

## Verification

- Bench before/after table — the angle's acceptance record. ✅ measured
  (step 4).
- Golden additions: CAS conflict → typed error surfacing as a
  retry/handoff row (not silent loss); TTL-expired resume →
  re-interpret fired; `objects_of_kind` / `list_requests` latency at
  10^5 rows. ✅ — landed as unit tests rather than golden cases, since
  none of them may become decision-table rows (DP-5/DP-7).
- Full suite green (145 tests today) + the embedding regression test.
  **Steps 2–5 gate, 2026-09-22: repo-wide `pytest` 551 passed / 1
  skipped, mypy clean; embedding regression delivered as
  `tests/test_orchestrator/test_async_surface.py` (runs on a live
  event loop, so a nested `asyncio.run` regrowth fails loudly).**
  Earlier gate for steps 2–4 alone: 544 passed.

## Non-goals (contract lines not crossed)

- No change to the control-loop structure, the four decision tables,
  DP-8, or the seven runtime objects.
- No service host / gateway here — embeddability only. The host that
  asserts identity is 05b's consumer.
- No distributed state between turns; per-request state stays on the
  envelope.
