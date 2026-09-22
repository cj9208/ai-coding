# Quantdesk — Design Rationale

Why the code is shaped this way. The * endeavor* arguments (is solo
quant viable, at what cost, through which channel) live in
`../personal-quant-trading-exploration.md`; the *plan* arguments
(milestone order, factor selection, validation gates) live in
`../personal-quant-bootstrap-plan.md`. This volume covers only what the
two leave open: the posture of the code itself.

## The data plane distrusts "downloaded once"

The Binance public archive is not an immutable store. Upstream
**replaces files after the fact** — the archive's own `updates/`
CHANGELOG is proof — so a file that was correct when fetched can be
wrong later without any local symptom. Three mechanisms answer that,
and they are the reason `raw/` is not just a downloads folder:

1. **Inventory at fetch time.** Every downloaded file's sha256 *as
   observed*, plus the remote checksum seen at that moment, goes into
   `raw/inventory.jsonl`. This is the durable claim: "on 2026-09-22
   upstream said this file was this hash."
2. **Replacements are events, not fixes.** `download --refresh` that
   sees a changed remote checksum refetches and writes a record with
   `replaced_at` set. A researcher can ask *which inputs moved under
   me*, which is the question no other crypto data tool lets you ask.
3. **`verify` checks three things, not one.** Local hash vs inventory,
   per-file row count against the expected time grid
   (`(max − min) // step + 1 == rows`), and — with `--remote` — current
   upstream checksums. Row counts and continuity matter because the
   public tooling histories (binance-public-data, freqtrade) show
   **silent missing/duplicate rows**; a checksum pass over wrong rows is
   a green light over a hole.

Why Parquet hive instead of a database? The hive *is* the index
(`symbol=/year=/month=` partitions, filesystem scan answers coverage
questions), it is the format every later consumer (polars, DuckDB, the
M3 kernel) reads natively, and it has no schema migrations to lose.
DuckDB is allowed only as a query view, never as truth — a claim that
must survive a tool change cannot live inside a tool.

## The ms→µs trap is a data fact, not a parsing accident

Binance switched kline timestamps from milliseconds to microseconds in
2025 without changing the file format. A silent unit switch upstream
means any naive loader spends 2025+ data in 1970. The convert layer
sniffs the unit (`max_raw >= 1e15`) per file rather than trusting a
cutoff date, because the honest posture is *the file tells you what it
is* — a date-based rule would have broken the day Binance had mixed a
switch across file boundaries. The same reason governs header sniffing:
klines and aggTrades ship headerless, fundingRate ships with a header,
and the first field being all-digits decides — file-declared again.

## The recorder exists because three columns of the factor table have
## no "download" cell

Extreme-funding reversal and post-liquidation reversion are the plan's
fast-band candidates, and the liquidation stream, OI beyond ~30 days,
and per-symbol premium/estimated-settle history exist **nowhere as
files**. The only way to ever own them is to accumulate them from day
one; hence `quant record`, a component whose value is literally a
function of uptime. Two consequences shaped it:

- **Append-only day buckets, no rewrites.** `recorded/<stream>/day=…/
  part-<HHMMSS>.<ns>.parquet`. A crash costs at most one flush window
  and never corrupts what came before — the private dataset is the
  scarcest asset in this plan and gets the most conservative write path.
- **A silent stream is a gap.** The first live smoke found this
  machine's network path completes the `fstream.binance.com` websocket
  handshake and answers ping frames, yet delivers **zero data frames**
  (spot WS and fapi REST on the same path work fine — the control
  experiments are the evidence, in commit 45460cc's message and
  `record.py`'s docstring). A connection that lies about being alive is
  exactly the failure mode a data plane must not eat quietly, so the WS
  loop runs a recv watchdog and logs `connected but silent` to
  `recorded/gaps.log` like any other dropout. The CLI `--silence-alert`
  made that path live-verifiable in 90 seconds; the default is 5
  minutes.

## Factors are as-of pure functions because the alternative is discipline

Every factor's first statement is a filter to `date <= as_of`. That is
the whole mechanism: future data is not discouraged, it is
**unreachable** — which is the same stance the repo already takes for
LLM outputs in the orchestrator (the harness decides, the model only
proposes). The corresponding test compares the function's output on the
full history vs the history truncated at `as_of`; identical output is
the acceptance, not a comment saying "we were careful."

One consequence worth owning: the factors re-filter and re-sort their
input on every rebalance date, which is slower than a sliding window.
At 2,900 daily bars × 100 symbols the waste is seconds; the guarantee
is worth more than the seconds. If a fast-band factor ever needs live
speed, the M3 kernel is where that optimization gets its complexity
budget.

## The shift convention has exactly one home

`screen.pnl` is the only place in the codebase where a decision moves
into a position: a target decided at bar t's close is traded at t+1
(cost charged that day) and earns the t+1 return. Signals never shift
anything; the engine never re-shifts. A factor that "wanted" to be
honest could not be dishonest, and a future M3 kernel importing the
same signal functions inherits the same property — which is what makes
the plan's decision-consistency test (replay vs paper: decisions may not
diverge) checkable at all.

## Costs default non-zero, the verdict reads only the stressed column

`COST_LEVELS = (10.0, 20.0)` — baseline taker and 2× — and
`passed_stressed` looks only at the worst level. This inverts the
industry default (backtest engines ship `fees=0` and rely on the user
to set them) because the observed failure rate of that default is, in
public reproductions, most published factors. A pass at baseline that
fails stressed is not a near-miss; it is a **cost fact** about the
factor, and the ledger records it as one row per cost level so the
2026-12 self cannot misremember which column looked good.

## The holdout is clamped in code

`run_screen` recomputes `end = min(end, last_bar − 365d)` and filters
the price frame before any factor sees it. There is no `--peek` flag,
because the plan's one-shot rule is only credible if the harness cannot
break it. The seal is also reported (`sealed from` in the CLI,
`sealed_from` in every manifest) so a reader of the evidence knows
exactly what the numbers do not include.

## The ledger and manifests are tracked files, not runtime logs

An unrecorded trial is, in the plan's words, "a lie told to our future
self", and deflated Sharpe is only as honest as the N the ledger
supplies. So `config/quantdesk/trial_ledger.csv` and
`config/quantdesk/runs/*.json` live under git, with code hash and git
rev in every row — the same posture as the orchestrator's
`golden_cases.jsonl`: evidence is source, data is data, and only one of
those two should be gitignored.

## Screening is polars-native, not vectorbt — a recorded deviation

The plan named vectorbt for screening; the implementation is ~350 lines
in `screen.py`. The argument is D-1's, applied one layer down: at the
slow band's daily grid, screening arithmetic is weighted returns minus
turnover costs, and those lines must agree bar-for-bar with the own
event kernel built in M3. Adopting vectorbt would have added a second
cost-semantics model to understand rather than removing one; it stays
on the comparison checklist (this is recorded in the plan's M1 bullet
as well, so the decisions document and the code tell the same story).

## The universe rank is a today-fact, stamped and frozen

`quant universe rank` intersects 24h quote volume with the current
TRADING snapshot — ranking *history* by *today's* activity is
survivorship bias wearing a leaderboard, and the code says so. What
makes it usable is that each screening run embeds the frozen symbol
list in its manifest: the claim is never "the top 100", only "the 100
names ranked on 2026-09-22, recorded here". The plan's open question 2
(delisted-pair depth) bounds what any of this can ever prove; the
honest posture is to accumulate dated snapshots and say so, not to
pretend the reconstruction is survivorship-free.
