# Personal Quant — Research Bootstrap Plan (M0–M3)

Status: **plan proposed 2026-09-22; M0 shipped and live-verified the same
day** (`src/quantdesk/`, `quant` CLI — archive download/verify, hive convert,
universe snapshots, BTCUSDT um 1m + funding round-trip clean), **recorder
(`quant record`) shipped**: funding and OI recorded live, liquidations feed
implemented and silence-watched but its positive path awaits a network where
`fstream.binance.com` pushes (TODO.md), **M1 factor layer shipped** (three
as-of factors + screening harness + trial ledger; the runs await the
crypto-native top-20 backfill — owner scope call, see M0 acceptance). Per
decision D-2 the living documentation moved to
the numbered set `docs/quantdesk/` (start at `00-overview.md`) — this
document stays the *scope and gates* authority. Companion to
`personal-quant-trading-exploration.md`, which owns the *why* (capital
arithmetic, channel walls, cost layers). This document owns the *how*
of one deliberately narrow slice: the **research pipeline only** —
data → factors → backtests → paper proof. Zero brokerage accounts,
zero capital, zero regulatory exposure; it is all offline compute on
free crypto data, so the exploration's §4 channel wall (the hard one)
is intentionally out of scope until a strategy passes the gates below.

The one-sentence core: **prove things on paper without lying to
yourself — by splitting the proof in two: statistical validity lives
in sealed out-of-sample backtests, operational validity lives in a
multi-month paper run; neither can substitute for the other.**

Two facts set the architecture. First, the statistical-power fact:
distinguishing Sharpe 1.0 from 0 at 95% confidence needs ~4–6 years
of independent observations (López de Prado MinTRL), so a 12-month
paper run of a weekly strategy proves *nothing* about returns — its
only honest jobs are decision-path fidelity and ops robustness.
Second, the data fact: every input this plan needs (1m/1s klines,
aggTrades, funding history) is free from Binance's archive, but the
fast-decaying signal feeds (liquidation stream, OI beyond ~30 days)
have **no public history at all** — which is precisely why recording
them ourselves from day one is a plan milestone, not an afterthought.

```text
                     pipeline layers and the two frequency bands
 ┌──────────────────────────────────────────────────────────────────────┐
 │ M0 binance-datatool-style download ─► Parquet hive (source of truth) │
 │    + verify: sha256 + ROWS + ts-continuity   + Universe (as-of,     │
 │    ► M1 recorder (live feeds no archive has): │ delisted pairs) ─┐  │
 │      forceOrder / OI-5m / funding-predicted  │                  │  │
 │      — accumulates a PRIVATE dataset         ▼                  │  │
 │ ──────────────────────────────────────────► factor layer        │  │
 │    pure functions, ONE shift convention: signal on bar t close, │  │
 │    fill at bar t+1 open; experiment manifest + TRIAL LEDGER     │  │
 │         │                        │                                │  │
 │         ▼                        ▼                                │  │
 │  vectorbt SCREENING        event kernel CONFIRM/PAPER (own-build) │  │
 │  baseline/stressed costs   same signal fn imported; replay =      │  │
 │  (fees default NON-ZERO)   paper = live shape; golden P&L diffs   │  │
 │         │                                                        │  │
 │         ▼                                                        │  │
 │  M2 validation: pre-registered hypothesis → walk-forward+purge →  │  │
 │  deflated Sharpe with ledger's real N → ONE-SHOT sealed hold-out   │  │
 │  (last 12 months, locked from M1 onward) → PASS/REJECT, no re-try  │  │
 └──────────────────────────────────────────────────────────────────────┘
```

## Decisions taken

- **D-1: own thin event kernel first, Nautilus as later reference.**
  Learning goal is to *find where the problems are* — fills, partials,
  latency assumptions, calendar/replay seams — by implementing them;
  switching to Nautilus once the problem landscape is understood is
  cheap because the signal layer is pure functions that either engine
  imports. Nautilus stays the checklist we compare our kernel against,
  not the starting dependency. (Train/serve-skew defense is unchanged:
  one signal function, backtest and paper call the same code.)
- **D-2: one plan document (this one), flat**; split into a `docs/quant/`
  numbered set only if/when `src/quantdesk/` (working name) ships M0.
- **D-3: frequency posture = both bands, different roles.** Slow band
  (signal daily, rebalance weekly-ish) carries the evidence-backed
  factors; fast band is *event-driven* (1–3 day half-life), viable only
  for signals we can verify on our own recorded data. Plain
  daily-turnover factor rotation is **deprioritized by cost arithmetic**:
  at ~10 bp per side, ~250 rotations/yr ≈ 5%/yr of costs, which a
  decayed (post-2023) cross-sectional edge cannot pay. "Weekly alpha is
  dead" is half-right: the *published* weekly anomaly decayed (net OOS
  Sharpe estimates 0.5–1.5, not 0), while the genuinely fast edges in
  crypto are event-shaped (funding spikes, liquidation cascades), not
  faster-scheduled versions of slow ones.

## Factor candidates, ranked by evidence × solo-accessible frequency

| Band | Candidate | Evidence state | Why it's in |
| --- | --- | --- | --- |
| slow | Top-100 weekly cross-sectional momentum, "avoid losers" form | Liu–Tsyvinski–Wu lineage; 2025 survey re-confirms t>2.5 but decaying; net OOS Sharpe realistically 0.5–1.5 | most durable published edge that survives at our cost level |
| slow | Time-series momentum + vol targeting on majors | post-cost Sharpe ~0.5–1.2 in papers | doubles as the *benchmark* other ideas must beat |
| fast | Extreme funding-rate percentile → 1–3d reversal | 2026 Binance microstudy confirms signal, short half-life; carry *mean* crowded (Ethena 30%→single digits) — only the extreme tail remains | data is free (funding history) AND the pulse is capacity-irrelevant at our size |
| fast | Post-liquidation dislocation reversion | academic support thin; **unverifiable without proprietary history** | the reason the M0 recorder exists — accumulate first, test in ~year 1 |
| — | triangular arb, MA crosses, calendar effects, book-imbalance | documented dead / weak / colocation games | rejected; recorded here so we don't re-try them later |

## Milestones

### M0 — Data plane (1–2 weeks)

CLI `quant download / verify / list / record` as the one entry point
(same posture as `ocr-backend download`: pinned, resumable, verified).

- Download layer mirrors [lostleaf/binance-datatool](https://github.com/lostleaf/binance-datatool):
  diff-sync against `data.binance.vision`, checksum per file, covering
  1m/1s klines + aggTrades + funding history for the universe.
- Storage: Parquet hive partitions `interval=…/symbol=…/year=…/month=…`
  under `data/quantdesk/`; DuckDB (if at all) strictly as a query view
  layer, never the source of truth; per-DuckDB guidance keep partitions
  ≥100 MB.
- **Verify = three things, not one**: sha256 manifest (archives get
  *retroactively replaced* upstream — the `updates/` CHANGELOG is
  proof), plus per-file row counts, plus timestamp continuity
  (freqtrade/binance-public-data issue histories show silent
  missing/duplicate rows). A `verify` failure blocks factor runs.
- Universe: reconstruct the *as-of* tradable set — delisted pairs must
  enter history (free archives under-cover them; accept partial and
  log the gap rather than pretending it's survivorship-free).
- **Recorder (goes live day one, runs forever)**: forceOrder
  (liquidations), 5m OI snapshots, funding predictions, mark-price
  streams → same Parquet conventions. Zero marginal cost, it is the
  only asset in this plan nobody else can't download — its value
  increases with uptime.
- Acceptance: one command rebuilds a sizeable universe × 4 years
  (amended 2026-09-22, owner scope call: top-100 → crypto-native
  top-20 — the ranking's stock/commodity perps have histories too short
  for any factor and are not what we would trade); truncating a file
  gets caught by `verify`; golden fixture (small month, byte-identical
  rebuild) in `tests/golden/`.

### M1 — Factor layer + screening harness (2–3 weeks)

- Signals are **pure functions** `(as-of dataframe) → positions`, with
  the single shift convention enforced at the boundary (signal on bar
  t close, fill at t+1 open — the Jesse/AlphaForge posture: make
  future data structurally unreachable, not a discipline problem).
- vectorbt for screening. Fees/slippage defaults *overridden non-zero*
  in our wrapper; two cost levels on every run: baseline
  (taker ~10 bp/side) and stressed (2×). **A factor passes only at
  stressed costs.**
  *(Deviation recorded at implementation, 2026-09-22: screening is a
  ~350-line polars harness in `quantdesk/screen.py` instead — at the
  daily grid the arithmetic must agree bar-for-bar with the M3 kernel
  later, so a second engine's cost semantics would be a thing to
  understand, not removed. vectorbt stays on the M3 comparison
  checklist.)*
- Experiment manifest per run (code hash, data range, params, cost
  settings — the AgentQuant `run_manifests` pattern) + **trial ledger**:
  append-only table, one row per variant ever evaluated, including
  failures. The ledger's N is the input to deflated Sharpe later; an
  idea's failures that aren't logged are lies told to our future self.
- Acceptance: per-factor unit tests incl. a "future invisibility"
  assertion; full re-run byte-identical; ledger + manifests in git.

### M2 — Validation protocol (1–2 weeks, then permanent process)

- **Pre-registration before any backtest**: markdown per hypothesis
  (economic reason, universe, rule, cost, primary metric, abandon
  threshold), committed *with timestamp* before results exist; changing
  any line after seeing output = new trial in the ledger.
- Walk-forward ≥3 folds with purge/embargo; numpy deflated-Sharpe
  (~20 lines, Bailey–López de Prado) using the ledger's real N.
- **One-shot hold-out: the last 12 months are sealed from M1 onward** —
  excluded from every fold, every tuning run, every glance. Read
  exactly once per hypothesis. Fail ⇒ the hypothesis is archived as
  REJECTED; no "tune once more and peek again".
- Gate to proceed to M3 (written before the hold-out is opened):
  stressed-cost OOS net Sharpe ≥ 0.5 **and** DSR ≥ 0.95 **and**
  parameter surface monotone under ±30% perturbation.
- Kill rule per idea: max ~20 variants / max ~40 h, then archive, next
  idea. (Per Carver: record every trial, pre-specify rules.)

### M3 — Own event kernel: replay → paper (1 week to build, 2–3 months to run)

- Thin deterministic kernel, repo-harness style: typed events
  (Bar/Trade, Order, Fill, Funding), budget-bounded loop, everything
  persisted (the orchestrator's shapes map 1:1); configurable
  fill/latency/fee models so the *cost assumption itself is testable*.
  Compare against Nautilus docs as the checklist (what did their fill
  model handle that ours ignores) rather than as the starting point.
- Three run modes, same code path: `replay` (Parquet history),
  `paper-live` (websocket quotes + simulated fills — record *modeled*
  vs eventual *actual* quote divergence), `live` (only ever after the
  §gates; not in this plan's scope).
- **Decision-consistency test** is the headline acceptance: replay the
  paper window through the backtest path and assert the *decision
  sequences* match 100% (fills may differ, decisions may not). Golden
  P&L/metrics JSON in git, diff-by-human on kernel changes — same
  ritual as `golden_cases.jsonl`.
- Execution accounting from day one: per-order signed shortfall vs
  arrival mid; realized cost ≤ 1.5× model is an exit criterion, since
  below it spread+fees (not impact) dominate at $10–100k sizes.
- Stats-free success criteria (per MinTRL honesty): 2–3 months or
  ≥100 signals, 0 crashes, 0 duplicate orders, cost model within
  tolerance. **The paper run is an ops proof; do not cite its P&L as
  evidence the strategy works.**

## What "paper-proven" finally means

A hypothesis is paper-proven when: pre-registered → passed stressed
walk-forward → passed the one-shot hold-out gate → ran ≥2 months of
paper with 100% decision fidelity and honest cost tracking. Only then
does the *next* decision exist, which is the one this plan deliberately
never touches: whether the channel/domicile layer (exploration §4)
permits risking $10–25k on it. Plausibly the pipeline will produce
zero survivors on its first pass — that outcome, recorded in the
ledger, is still the paper proof doing its job.

## Open questions (parked, not blocking)

1. Fast-band verification without liquidation *history* until our
   recorder accumulates ~a year — does the extreme-funding factor
   alone justify fast-band work in year 1? (current posture: yes, it
   has free history)
2. Universe reconstruction depth: how many delisted Binance pairs are
   actually retrievable; if sparse, bound the survivorship bias
   direction instead of eliminating it.
3. `quantdesk` vs a name matching repo conventions — decide at M0
   commit, remember the `packages` list + `uv pip install -e .` step.

## Sources

Engineering: [binance-datatool](https://github.com/lostleaf/binance-datatool) ·
[binance-public-data (archives get updated)](https://github.com/binance/binance-public-data) ·
[DuckDB partitioning](https://duckdb.org/docs/lts/data/partitioning/partitioned_writes.html) ·
[Parquet+DuckDB for quants](https://www.alphanova.tech/blog/parquet-and-duck-db) ·
[vectorbt](https://vectorbt.dev/api/portfolio/base/) ·
[freqtrade backtesting limits](https://www.freqtrade.io/en/stable/backtesting/) ·
[Nautilus data catalog](https://nautilustrader.io/docs/latest/concepts/data/) ·
[AlphaForge (factor registry + next-bar-open)](https://github.com/warren618/AlphaForge) ·
[uningenieur end-to-end writeup](https://uningenieur.fr/posts/crypto-trading-bot/) ·
[golden-master regression](https://quantmemo.com/concepts/golden-master-regression-tests)
Factors: [NBER w25882](https://www.nber.org/system/files/working_papers/w25882/w25882.pdf) ·
[Crypto as investable asset class 2025 survey](https://arxiv.org/html/2510.14435v1) ·
[momentum "skip losers"](https://link.springer.com/article/10.1007/s11408-025-00474-9) ·
[extreme funding → intraday reversal (Binance microstudy)](https://www.researchgate.net/publication/413749377_Extreme_Perpetual_Futures_Funding_and_Subsequent_Bitcoin_Returns_Intraday_Evidence_from_Binance) ·
[Ethena funding compression](https://docs.ethena.fi/protocol-overview/risks/funding-risk) ·
[triangular arb dead](https://fis.uni-bamberg.de/bitstreams/8b9ae900-017a-4bed-94b9-609c16e89945/download)
Validation: [MinTRL/PSR explainer](https://portfoliooptimizer.io/blog/the-probabilistic-sharpe-ratio-bias-adjustment-confidence-intervals-hypothesis-testing-and-minimum-track-record-length/) ·
[Deflated Sharpe](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) ·
[pure-noise Sharpe calibration](https://www.quantt.co.uk/resources/sharpe-ratio-of-pure-noise) ·
[walk-forward practice](https://arxiv.org/html/2512.12924v1) ·
[Binance spot testnet → demo mode](https://developers.binance.com/en/docs/products/spot/demo-mode/general-info) ·
[paper-fill optimism (freqtrade issues 3685/3389)](https://github.com/freqtrade/freqtrade/issues/3685) ·
[arrival-cost methodology](https://wholesale.banking.societegenerale.com/en/news-insights/all-news-insights/news-details/news/trading-costs-versus-arrival-price-intuitive-and-comprehensive-methodology/) ·
[Carver on process discipline](https://bettersystemtrader.com/026-robert-carver/)
