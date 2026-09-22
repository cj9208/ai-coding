# Quantdesk — Implementation Guide

What `src/quantdesk` actually is today: 9 modules, ~1900 lines, M0–M1 of
the bootstrap plan. This document walks the shipped code —
responsibilities, every tuned constant, the upstream format traps
already absorbed — and the points where reality diverged from the plan
and from `01-design-rationale.md`. When the two disagree, **this
document is right about the code, and 01 is right about the intent**.

## Module map

```text
        THE DATA PLANE (M0)                        THE RESEARCH PLANE (M1)
 config.py    122   paths + DATASETS   signals.py   220   load_* + 3 as-of factors
 archive.py   320   diff-sync +        screen.py    353   shift/costs/holdout/ledger
                     inventory                │                 │
 convert.py   214   raw zip ->                 └──────┬────────┘
                     hive month partition             ▼
 store.py     150   list / verify           cli.py   347   `quant` — one entry point
 universe.py  114   snapshots + rank
 record.py    297   the three unarchived feeds
```

Data flows strictly downward: `raw/` (verified bytes) → `parquet/` hive
(source of truth) → `signals.load_*` (frames) → `screen.run_screen`
(metrics) → `config/quantdesk/` (evidence). Nothing reads `raw/` twice
in different ways, and nothing in the research plane can reach the
network.

## `config.py` — paths and the dataset registry

- `paths()` resolves at **call time** (`QUANTDESK_DATA_DIR` env override,
  else `utils.paths.data_dir("quantdesk")`) — tests repoint the env var
  and every module follows without monkeypatching.
- `Dataset` is a NamedTuple: `market`/`kind`/`interval` drive the URL,
  `monthly`/`daily` the granularity plan, `time_col`/`step_us` the grid
  check. `dir_slug` = name with `/` → `_` for the hive directory.
- `DATASETS` (7 entries): `spot_klines_1m`, `spot_klines_1s`,
  `spot_klines_1d`, `um_klines_1m`, `um_klines_1d`,
  `um_funding_rate` (monthly-only, `step_us` = 0 → the interval-aware
  `check_funding_grid`, see below),
  `spot_agg_trades` (`step_us=0` → grid check skipped).
- kline `step_us` from `_SPOT_KLINE_STEP`: 1m = 60 000 000,
  1s = 1 000 000, 1d = 86 400 000 000 µs.

**Adding a dataset is one `DATASETS` entry** (+ its columns in
`convert.CSV_SCHEMAS` if the kind is new). The registry mirrors
`ocr_backend.models.MODELS` on purpose.

## `archive.py` — the download layer

- `PlannedFile` derives *everything* from the dataset entry: `key`
  (`<dataset>/<symbol>/<granularity>/<stamp>` — the inventory join
  key), `filename` (`SYM[-interval|kind]-stamp.zip`), `relative_path`
  (upstream layout, also our `raw/` layout), `url`, `local_path`.
- `http_get` is **the one network seam** (tenacity: 3 attempts,
  exponential 1–10 s — but `RemoteMissing` (404) is never retried: a
  missing month is a durable answer, and retrying it tripled the cost
  of every gap during the top-20 backfill; callers turn it into a
  `missing` action, not an error). Tests fake this function only.
- `fetch_checksum(url)` reads the `.zip.CHECKSUM`; together with
  `sha256_file` it produces the two recorded hashes per file:
  `sha256` (local bytes) and `remote_sha256` (upstream's claim *at
  fetch time*).
- `_month_complete(month)`: monthly archives land the **first Monday
  after month end**; until then `plan()` emits daily files
  (`days_of` = every day up to *yesterday* — archives publish T+1).
- `fetch(planned, refresh_remote)` implements the replacement rule:
  with `--refresh`, a local file that still matches the inventory but
  whose *remote* checksum moved is re-fetched and the new record gets
  `replaced_at` — while the old `remote_sha256` is preserved so the
  event stays visible. Outcomes: `fetched | skipped | replaced |
  missing | mismatch`.
- `inventory.jsonl` is append-only; `load_inventory()` is
  last-write-wins by `key`.

## `convert.py` — raw zip → hive

One output per (dataset, symbol, month):
`parquet/<dir_slug>/symbol=SYM/year=YYYY/month=MM/part-<YYYY-MM>.parquet`.

- `month_sources`: **monthly wins**; if absent, all daily records for
  that month, concatenated, sorted, `unique(time_col, keep="first")`.
  `convert_month` deletes stale `part-*.parquet` in the partition first,
  so daily parts can never coexist with the monthly rebuild.
- Two live-probed traps absorbed here, invisible downstream:
  - **µs/ms switch**: `MICROSECONDS_CUTOFF = 1e15` — the time column's
    `max()` decides the unit *per file* (2025+ archives are µs, earlier
    ms); everything is stored as tz-aware UTC `Datetime("us")`.
  - **header sniffing**: `_sniff_header` = first field of the first line
    is not a digit (fundingRate has a header; klines/aggTrades don't).
    Headerless files are renamed positionally against
    `KLINE_COLUMNS` (12 fields, last is `ignore`, dropped) /
    `AGG_TRADE_COLUMNS` / `FUNDING_COLUMNS`
    (`calc_time, funding_interval_hours, last_funding_rate`).
- `read_csv` parses everything Utf8 (`infer_schema_length=0`) and casts
  deliberately via `_INTS`/`_FLOATS` — no schema inference anywhere.
- The write is a **pure function of verified raw bytes**: "same month,
  two answers" can only mean upstream replaced the archive, which the
  inventory already flags.

## `store.py` — coverage and integrity

- `scan_coverage()` walks the hive directories — the filesystem *is*
  the index, no catalog.
- `verify(remote)` runs the three checks in the order their failure
  modes bite: `check_local_hashes` (disk rot; kinds `inventory` /
  `local-hash`), the grid check per built month — `check_grid`
  (`(max-min)//step_us + 1 == rows`, exact for fixed-step bars) or,
  for funding, `check_funding_grid` — and with `--remote`,
  `check_replacements` — a moved upstream checksum is reported as
  `REPLACED … re-download with --refresh`, never silently fixed.
- **Funding has no fixed step** (trap found by the full backfill, not
  by the first probe): the exchange compresses settlement intervals
  per contract (8h → 4h → 2h — on the top-20 corpus: 94 092 8h,
  13 196 4h, 98 2h gaps) and *skips* single settlements outright
  (ENA/HYPE/TAO all lack 2026-06-24T04:00, verified absent from
  Binance's own zips), while `calc_time` carries ms jitter. So the
  span-vs-rows math cannot hold; the honest invariant is per-gap:
  every gap must be a whole multiple (±1 s) of at least one endpoint
  row's `funding_interval_hours`. That catches off-grid corruption
  and tolerates upstream skips — a dropped row and a skipped
  settlement are indistinguishable by timestamps, so row fidelity
  rests on `check_local_hashes` + `convert`'s determinism instead.
- Nonzero exit on any issue is deliberate: `verify` is a gate, not a
  report.

## `universe.py` — dated symbol lists

- `fetch_snapshot` accepts status `TRADING` **and `CLOSE_UP`** (a pair
  winding down still traded during the period a snapshot describes);
  USDT-quoted only. Spot via `data-api.binance.vision` (the
  CloudFront-fronted host — `api.binance.com` is geo-blocked in more
  places); um via `fapi.binance.com`.
- Snapshots: `universe/<market>-<YYYY-MM-DD>.json`, one file per day,
  never rewritten — the *series* is the survivorship antidote.
- `rank_by_quote_volume(market, top)` intersects today's 24 h ticker
  with the latest snapshot's TRADING set — a *today* fact, which is why
  every screen run stamps the frozen list into its manifest.

## `record.py` — the three unarchived feeds

- Streams (`STREAMS`): `liquidations` (WS
  `wss://fstream.binance.com/stream?streams=!forceOrder@arr`),
  `funding` (REST `/fapi/v1/premiumIndex`, keeps the estimated/premium
  fields the monthly archive throws away), `open_interest`
  (`/fapi/v1/openInterest` per symbol — the exchange's own history
  endpoint only looks back ~30 days).
- `RecorderState` defaults: `funding_every=60 s`, `oi_every=300 s`
  (the 5-minute OI grid), `flush_every=30 s`, `recv_timeout=15 s`,
  `silence_alert=300 s`.
- Writes: `DayBucketWriter` buffers per (stream, day); each flush
  **appends** `recorded/<stream>/day=…/part-HHMMSS.<µs>.parquet`; empty
  flush writes nothing (a zero-row file would trip the continuity scan
  for no reason). A crash loses at most one flush window.
- Gaps: WS dropouts log to `recorded/gaps.log` and reconnect with
  1→60 s backoff; a *silent* connection is logged the same way once
  `silence_alert` seconds pass with zero frames despite answered pings
  — the verified condition on this machine's network path.
- `run()` bounds its own exit: `asyncio.wait_for(gather(tasks),
  remaining)` + cancel-all in `finally` + final flush, because a quiet
  websocket's read loop cannot observe the deadline by itself.

## `signals.py` — the factor layer

- Loaders read the hive, never the network:
  `load_closes(dataset, symbols)` pivots every
  `part-*.parquet` under the dataset into one wide daily frame
  (`date` + one column per symbol; missing history stays null — a
  factor cannot score a symbol into existence). `_symbol_of` walks
  ancestors for `symbol=` because the hive leaf's parent is `month=`.
  `load_funding(symbols)` returns the long `(symbol, calc_time,
  last_funding_rate)` frame.
- **Every factor's first statement filters to `date <= as_of`** — the
  future is unreachable code. All return `dict[symbol → weight]`.
- `MIN_OBS_DEFAULT = 250` bars before a symbol can score at all.
- `csm` `cross_sectional_momentum(wide, as_of, lookback=180, skip=5,
  hold=20)`: score = `close[n-skip-2] / close[n-lookback-1] - 1`; long
  top `hold`, equal weight, rest cash. Long-only by design ("avoid
  losers" form — daily-turnover shorts cannot pay 10 bp/side).
- `tsm` `time_series_momentum(wide, as_of, lookback=90, vol_window=30,
  target_vol=0.20)`: sign of the trend × `min(1, target_vol /
  annualized_vol)`, whole book capped at gross 1.
- `funding` `funding_reversal(funding, as_of, window_days=30,
  extreme=0.9, min_rows=90, fresh_days=3)`: mid-rank percentile
  `(below + 0.5·tied)/n` of the latest *settled* rate inside each
  symbol's trailing window (mid-rank so a flat series sits at 0.5, not
  in both tails — caught by test); percentile ≥ `extreme` → short side,
  ≤ 1−`extreme` → long side; equal weight per side, gross 1
  (½ per side). `fresh_days` refuses to act on a stale feed.
- Registries: `SLOW_FACTORS` (`csm`, `tsm` — take the price frame),
  `FAST_FACTORS` (`funding` — takes the funding frame),
  `FACTORS = {**SLOW, **FAST}`.

## `screen.py` — the screening harness

Constants: `BP = 1e-4`; `COST_LEVELS = (10.0, 20.0)` bp/side
(baseline / stressed — the plan's cost posture);
`HOLDOUT_DAYS = 365`; `PASS_SHARPE = 0.5` (verdict computed **only** at
the stressed level); `LEDGER_DIR = REPO_ROOT/"config"/"quantdesk"` —
evidence is tracked, data is not, so the ledger and manifests live with
the source, not under `data/`.

- `code_hash()` — sha256 over the sorted contents of every
  `src/quantdesk/*.py` (first 16 hex); `git_rev()` — `git rev-parse
  HEAD`. Both land in every manifest, so a result can always be tied to
  the exact code that made it.
- `ScreenSpec` → `run_id` = `{factor}-{start}_{end}-{sha256(canonical
  json of factor/params/symbols/rebalance/start/end)[:10]}` — the same
  inputs cannot produce a second identity.
- **The shift convention lives only here** (`pnl`): a decision made at
  bar *t*'s close is executed at *t+1* — the L1 turnover is charged on
  *t+1* and the held weights earn *t+1*'s return. The warm-up days
  before the first decision are skipped, so `metrics.from` reports the
  real scored window.
- `run_screen`: `sealed = last_data_day − HOLDOUT_DAYS`;
  `end = min(spec.end, sealed)`; **the wide frame is filtered to
  `≤ end` before anything reads it** — the holdout is clamped in code,
  there is no flag to peek. Fast-band factors run through
  `_funding_schedule` (rebalance grid still from the price calendar).
  Each cost level gets its own `metrics(pnl(...))`.
- `metrics`: annualized Sharpe (√252), CAGR from terminal equity,
  max drawdown, `turnover_pa`, `cost_drag_pa`, `days`, `from`/`to`.
- `manifest_payload(result)` contains **no clock** — rerunning the same
  spec produces a byte-identical `runs/<run_id>.json` (the property a
  test asserts; it is the reproducibility proof an agent can check).
  `record_run` writes that manifest and appends one ledger row per cost
  level to `trial_ledger.csv` (columns: run_at, run_id, factor,
  cost_bp, sharpe, cagr, max_drawdown, turnover_pa, days,
  passed_stressed, params, n_symbols, data_start, data_end,
  sealed_from, code_hash, git_rev). The ledger write resolves
  `LEDGER_FILE` at call time and the row's `run_at` is the only
  wall-clock fact recorded.

## `cli.py` — the `quant` entry point

Seven subcommands (`download / convert / list / verify / record /
screen / universe sync|show|rank`); the full flag reference is
`03-usage.md`. Two wiring notes:

- `download`/`convert` share `add_range` — `--symbols` **or** `--top N`
  (from the latest snapshot), `--since`/`--until` `YYYY-MM`, until
  defaults to last month. Rerunning is a no-op.
- `screen` accepts `--rank N` (ranks live, prints the today-fact
  warning to stderr, freezes the list into the manifest) and
  repeatable `--set K=V` factor params (`_parse_sets`: int if all
  digits, else float).

## Tests

`tests/test_quantdesk/` (26 tests, all offline — `archive.http_get`,
`websockets.connect` and the hive are faked):

| File | What it pins |
| --- | --- |
| `test_archive.py` | URL/path derivation, diff-sync outcomes, replacement event recorded not swallowed |
| `test_convert.py` | ms/µs sniffing per file, header sniffing, monthly-beats-daily, de-dup |
| `test_store.py` | grid math (bars + funding: compression, skip, jitter tolerated; off-grid gap flagged), local-hash rot detection, remote REPLACED detection |
| `test_record.py` | parsers, append-only day buckets, deadline-bounded `run()` exit, silence → gap log |
| `test_signals.py` | winner picks, **future-blindness** (full vs truncated frame identical), min-obs exclusion, gross caps, flat-funding not in either tail |
| `test_screen.py` | exact shift-day arithmetic of `pnl`, holdout clamp bounds `to`, byte-identical rerun manifests, CLI end-to-end against a seeded hive |

## Deviations from the plan (and why)

- **vectorbt replaced by ~350 lines of polars/stdlib screening
  harness.** The plan listed vectorbt for M1; at implementation the
  needed semantics (single shift convention, holdout clamp, ledger)
  were a fraction of what vectorbt brings, and the M3 kernel must agree
  with the screener *bar-for-bar* — an interpreter over a library's
  internals cannot be that reference. vectorbt stays on the M3
  comparison checklist.
- **Daily bars added to the registry** (`*_klines_1d`): D-3 chose the
  two bands (daily-turnover fast, weekly slow), and the slow band's
  screening needs daily closes — the archive provides them for free.
- **M0.5 recorder shipped inside M0's window.** The plan sequenced it
  after M0's core; it moved forward because every day it doesn't run is
  a day of unarchivable liquidation/funding/OI history lost — the only
  dataset in the plane whose backfill is impossible.
- **`funding_reversal` uses mid-rank percentile**, not
  `count(≤x)/n`. Not a plan deviation (the plan didn't specify) — a bug
  the test class caught: with `count(≤x)/n` a *constant* funding series
  scores 1.0 and appears in both tails simultaneously.
