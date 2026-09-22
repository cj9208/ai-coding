# Quantdesk — Usage

The full `quant` CLI reference, the recipes already executed live, and
the two extension points (add a dataset, add a factor). Everything runs
from the repo root with `uv run quant …`; all paths land under
`data/quantdesk/` regardless of cwd (`QUANTDESK_DATA_DIR` overrides).

## Subcommands

### `quant download` — diff-sync archives into `raw/` + inventory

```
--datasets CSV     registry names from config.DATASETS (required):
                   spot_klines_1m | spot_klines_1s | spot_klines_1d |
                   um_klines_1m | um_klines_1d | um_funding_rate |
                   spot_agg_trades
--symbols CSV      explicit symbol list            ── one of these:
--top N            first N from the latest snapshot (--market)
--market {spot,um} which snapshot to read (default um)
--since YYYY-MM    required
--until YYYY-MM    default: last completed month
--no-daily         don't cover the not-yet-published current month with daily files
--refresh          re-check upstream checksums; act on replacements
```

Per-file progress to stderr (`fetched` / `replaced` / `missing` /
`mismatch`), summary line to stdout:
`download: fetched=512  missing=41  replaced=0  skipped=8`.
`missing` is information, not failure — recent listings simply have no
older months. Exit code 0 unless a checksum mismatch aborted a file.

### `quant convert` — raw zip → hive month partitions

Same range flags as `download` (no `--no-daily`/`--refresh`).
Idempotent: rebuilds each (dataset, symbol, month) partition from the
verified raw bytes; prints `built <ds>/<sym>/<month> rows=N from
monthly|daily-K`. Run it after every download; running it twice changes
nothing.

### `quant list` — coverage table

One row per (dataset, symbol): months built, span, total rows. Reads
the hive directories only — costs a parquet header-read per partition.

### `quant verify [--remote]` — the integrity gate

Checks local hashes + per-month grid continuity always; `--remote` adds
the upstream-checksum sweep (one HTTP request per inventory record —
slow; run it before trusting a corpus, not after every command).
Prints one line per issue to stderr; **exit 1 if any** — it is a gate.

### `quant universe sync | show | rank`

- `sync` — fetches spot + um TRADING USDT lists, writes
  `universe/<market>-<today>.json`. Run it on a schedule (daily) — the
  series is the point; a single snapshot cannot backtest against.
- `show` — latest snapshot size and day per market.
- `rank --market {spot,um} --top N` — today's top-N by 24 h quote
  volume (intersected with the latest snapshot). CSV of symbols to
  stdout (shell-composable), volumes to stderr. **A today-fact**: any
  run that freezes it must record the day — `quant screen --rank`
  prints the warning and the manifest stores the list.

### `quant record` — accumulate the three unarchived feeds

```
--streams CSV      liquidations,funding,open_interest (default: all three)
--symbols CSV      per-symbol polls (open_interest needs this or --top)
--top N            first N from the latest um snapshot
--minutes F        stop after F minutes (default: until ^C)
--silence-alert F  seconds of zero frames on a live WS before logging a gap (default 300)
```

Rows flush every 30 s to `recorded/<stream>/day=…/part-*.parquet`;
every dropout *and every alive-but-silent stream* appends to
`recorded/gaps.log`. On this machine's network the liquidations WS
connects, answers pings, and pushes nothing — the gap log is expected;
the funding/OI REST polls work fine. See TODO.md for the owed
positive-path verification.

### `quant screen` — run a factor through the harness

```
--factor {csm,tsm,funding}   required
--dataset NAME               price dataset (default um_klines_1d)
--symbols CSV | --rank N     the universe — one is required
--since YYYY-MM-DD           required
--until YYYY-MM-DD           required; CLAMPED to the seal (see below)
--rebalance N                decide every N bars (default 5)
--set K=V                    factor param, repeatable (--set lookback=180 --set hold=20)
--note TEXT                  free text into the manifest
```

Example output (synthetic-scale, from the test-adjacent pilot):

```text
run_id        csm-2022-01-01_2026-06-30-3f2a…
sealed before 2025-06-24 (holdout unread)
 cost_bp  sharpe    cagr   maxDD turnover/pa  cost/pa  days
      10     0.82   9.1%    -21%          9.6    1.9%   738
      20     0.61   6.7%    -23%          9.6    3.9%   738
stressed-cost verdict: PASS (screening only — not proof)
ledger += rows; manifest -> config/quantdesk/runs/csm-….json
```

What the harness guarantees (see `01` for why): the factor only ever
sees `date <= as_of`; decisions fill next bar with costs; the last 12
months of data are clamped out of *every* read (`--until` cannot
extend past the seal); the run wrote ledger rows + manifest whether it
passed or failed.

## Live-proof recipes (already executed)

**Full round-trip (M0 acceptance, 2026-09-22):**

```bash
uv run quant universe sync
uv run quant download --datasets um_klines_1m --symbols BTCUSDT --since 2024-01 --until 2024-03
uv run quant convert  --datasets um_klines_1m --symbols BTCUSDT --since 2024-01 --until 2024-03
uv run quant list
uv run quant verify --remote
```

**Crypto-native top-20 backfill (M1 data, 2026-09-22):**

```bash
UNIVERSE=data/quantdesk/universe
uv run quant universe rank --market um --top 100 > $UNIVERSE/rank-um-top100-2026-09-22.csv   # stdout is one csv line
# freeze the universe: first 20 CRYPTO-NATIVE names in ranking order,
# skipping tokenized stock/commodity perps (XAU/XAG/CL/SOXL/SNDK/SPCX/
# SKHYNIX/MSTR/MU/INTC/KORU/CRCL/…, mostly listed 2025-26) — the owner's
# scope call: large caps only. Result (2026-09-22 ranking), recorded in
# $UNIVERSE/screen-universe-um-top20-majors-2026-09-22.csv:
#   BTC ETH SOL ZEC XRP DOGE 1000PEPE NEAR SUI HYPE
#   BNB UNI TAO ENA ADA AVAX WLD ARB LINK LTC
SYMS=$(cat $UNIVERSE/screen-universe-um-top20-majors-2026-09-22.csv)
uv run quant download --datasets um_klines_1d,um_funding_rate --symbols "$SYMS" --since 2021-01
uv run quant convert  --datasets um_klines_1d,um_funding_rate --symbols "$SYMS" --since 2021-01
```

(The rank file is a *frozen* universe — the selection rule and its day
belong in the screening `--note`; the frozen CSV itself sits under
`data/quantdesk/universe/` beside the snapshots — gitignored like all
of `data/`, but reconstructible from the rank file and, for the audit
trail, byte-identical to the `spec.symbols` list committed in every
manifest under `config/quantdesk/runs/`. ~2.7k planned files,
`missing` months for late listings are normal and cost one 404 each
since ceb595e. Observed throughput ≈ 1.4 s/file for old names.)

**Screening the three pre-registered hypotheses (M1 acceptance):**

```bash
SYMS=$(cat data/quantdesk/universe/screen-universe-um-top20-majors-2026-09-22.csv)
uv run quant screen --factor csm --symbols "$SYMS" --since 2022-01-01 --until 2026-06-30 \
                    --set hold=5 --note "m1 pre-reg: csm, 20-major universe 2026-09-22, hold=top quartile"
uv run quant screen --factor tsm --symbols "$SYMS" --since 2022-01-01 --until 2026-06-30 \
                    --note "m1 pre-reg: tsm defaults, 20-major universe 2026-09-22"
uv run quant screen --factor funding --symbols "$SYMS" --since 2022-01-01 --until 2026-06-30 \
                    --note "m1 pre-reg: funding defaults, 20-major universe 2026-09-22"
```

**Reproducibility check** (the acceptance property): rerun one screen
unchanged, then `git diff config/quantdesk/runs/` — the manifest must
be untouched (only new ledger *rows* in the csv). A changed manifest
means the result depended on something the spec didn't name.

## Reading the evidence

- `config/quantdesk/trial_ledger.csv` — one row per (run, cost level),
  appended on every screen, failures included. This is the multiple-
  testing record the M2 gates consume: `count(run_id)` is the number of
  variants tried.
- `config/quantdesk/runs/<run_id>.json` — the manifest: spec, symbols,
  `sealed_from`, metrics per cost level, `code_hash`, `git_rev`,
  `note`. Everything needed to re-execute or dispute a number.

## Extending

**Add a dataset**: one `DATASETS` entry in `config.py` (market/kind/
interval/step_us/time_col) — URLs, filenames, planning and grid checks
all derive from it; plus its columns in `convert.py` if the kind is
new. No other file changes.

**Add a factor**: one pure function in `signals.py` — first statement
filters to `as_of`, signature `(frame, as_of, **params) ->
dict[symbol→weight]` — registered in `SLOW_FACTORS` or `FAST_FACTORS`.
`quant screen --factor <name>` picks it up; `--set` reaches its
defaults. Two tests are mandatory, mirroring the existing ones: a
winner-pick test and a **future-blindness** test (truncate the frame
after `as_of`; the positions must be identical).

**Add a cost discipline**: don't. `COST_LEVELS` and the stressed-only
verdict are the posture; changing them changes what "PASS" has ever
meant across the ledger.

## Exit codes

| Command | 0 | nonzero |
| --- | --- | --- |
| `download` | range synced (missing months reported, not failures) | checksum mismatch aborted (no durable record for that file) |
| `convert` / `list` / `universe` | always | bad args / no snapshot |
| `verify` | clean | any issue (gate semantics) |
| `screen` | run recorded | bad args / no hive partitions |
| `record` | clean stop or ^C (both flush) | unknown stream |
