# Personal Quant Trading — Feasibility Exploration

Status: **desk research, 2026-09-22, no positions taken.** Sources are
public pricing pages, academic studies, and 2025–2026 regulatory
announcements (links at the bottom); numbers carry their as-of dates
because both the tooling market and the China-side access rules moved
fast this year. Nothing here is investment or legal advice — the point
is to price the *plumbing* and rank the *obstacles* before writing a
line of strategy code.

The one-sentence finding: **data, compute, and backtest frameworks are
nearly free and already solved for a solo operator; what actually
decides the outcome is (1) brokerage/exchange channel access given a
mainland-China identity, (2) the arithmetic that small capital makes
even a top-decile return irrelevant as income, and (3) the 3–5 year
ops timeline.** The obstacle ranking is
`account & domicile > data > strategy` — the reverse of how the
question is usually asked.

```text
            the four layers, and where each one is easy or fatal
 ┌────────────────────────────────────────────────────────────────────┐
 │ 1 DATA        2 COMPUTE+FRAMEWORK     3 EXECUTION CHANNEL          │
 │  crypto: ~$0  laptop does daily/      US: Futu/Tiger/Longbridge    │
 │  (Binance     minute fully; vectorbt   cut off for mainland         │
 │  Vision full   sweeps millions of      clients (2026-06); IBKR      │
 │  history);     params; freqtrade/      requires offshore proof of   │
 │  US daily      Nautilus production-    residence (2025-09);         │
 │  $0–50/mo      grade; backtrader EOL   Alpaca: no CN residents      │
 │                                        Crypto: exchanges KYC-restrict│
 │        EASY              EASY          CN residents; legal risk     │
 │                                           concentrates in off-ramp  │
 │                                           FATAL — the real gate     │
 │ 4 PROFITABILITY MATH                                                   │
 │  retail net-profit base rate 1–5%; Sharpe-1.5 systems see 30%        │
 │  drawdowns as a *normal* tail; 20% on $10k = $2k/yr < boredom        │
 │  benchmark (BTC carry ~7.9%, 2y UST 4.2%, 2026-08)                   │
 │  → solo-viable only as capacity-capped narrow strategies             │
 │    (weekly, long-tail, sub-$M capacity) or risk-free-ish carry        │
 │    practice runs                                                      │
 └────────────────────────────────────────────────────────────────────┘
```

## 1. The capital arithmetic first

Everything else is detail until this clears. A genuinely good solo
result — 20% net, high Sharpe — is a top-1% outcome (Barber–Odean:
among 66k day-trading accounts 1984–2002, ~1% were predictably
profitable net of costs and 93% quit within five years; CFTC 2024
futures/forex research: 70–80% of retail accounts unprofitable; EU
brokers must disclose 74–89% of CFD clients lose).

| Capital | @20% net/yr | Read |
| --- | --- | --- |
| $10k | $2k | pocket money, not income |
| $100k | $20k | one normal 30% drawdown (−$30k at Sharpe≈1.5, 20% vol) erases 1.5–2 years of "income" |
| $500k | $100k | the level where returns start to look like a salary |
| $1M | $200k | fund-grade consistency needed to keep it |

The boredom benchmark: in 2026-08 BTC basis/carry ran ~7.89% while the
2-year Treasury paid 4.19%. A strategy's alpha must clearly beat doing
nothing risky-or-not, or the evenings aren't worth it. Conclusion the
industry converges on: **small capital + high Sharpe = allowance;
living off returns needs $300–500k of genuinely losable money** —
unless the edge lives in capacity-capped niches ($10–100k can earn
real absolute dollars there, at the price of heavy ops; §4).

## 2. Data — the layer that is essentially free to start

US equities (backtest-grade, mid/low frequency): **$0–50/mo covers the
whole research phase.**

- Massive (Polygon.io renamed 2025-10-30): free tier 2y history;
  $29/mo 5y 15-min-delayed; $199/mo 20y + real-time full trades/quotes.
- Alpaca: free = IEX-only feed (~2% of consolidated volume — high/low
  and spread stats are distorted); SIP real-time is $99/mo.
- Survivorship-bias-free history is the one thing you pay for
  separately: Norgate $270–630/yr (delisted stocks, index membership,
  fundamentals). Fundamentals themselves are free via SEC EDGAR XBRL.
- Tick/L2 is the jump in orders of magnitude: Databento usage-based
  from $0.40/GiB (subscriptions $199–4,500/mo at the institutional
  end); non-professional direct exchange subs via IBKR are cheap
  ($1.50–$25/mo) but redistribution licensing is not.

Crypto: **a solo researcher gets essentially everything free.**
`data.binance.vision` hosts full spot+perp history — trades,
aggTrades, 1m *and 1s* klines, funding rates, bookDepth snapshots;
Bybit/BitMEX mirror the posture; funding-rate/perp-basis history (the
cheapest derivative signal) is a free API call. Tick/L2 archives
(Tardis, minimum order ~$300, download-only during subscription;
CoinAPI $79–599/mo) exist but are only needed once microstructure
work starts. Kaiko/L2 institutional aggregation starts ~$1k/mo — a
market a solo operator should not enter.

Quality traps that cost more than the data itself (each one has burned
someone's backtest):

1. **Survivorship bias** — free feeds only list surviving symbols; a
   2022 St. Gallen study shows delisting bias materially inflates
   crypto portfolio returns.
2. **Adjustments** — Yahoo/Tiingo/Massive adjusted closes disagree;
   back-adjusted series drift when new splits/dividends appear. The
   correct posture is unadjusted prices + a corporate-actions table +
   point-in-time re-adjustment.
3. **Timestamps** — ET vs UTC vs DST on the US side; kline open-time
   vs close-time conventions differ per venue.
4. **Silent gaps & rewrites** — minute gaps after hours, 500-row API
   caps, ticker renames quietly rewriting history; full pulls need
   row-count/checksum verification (same instinct as golden fixtures).
5. Crypto spot volume carries wash-trading contamination.

Storage reality (order of magnitude): US minute data 5y full-market ≈
50–80 GB as Parquet; crypto 1s klines for the top 100 pairs 5y ≈
150 GB Parquet; L2 book deltas for *one* pair 5y compresses to 2–3 TB.
Parquet + DuckDB/Polars on a laptop handles everything up to the last
row.

## 3. Compute and frameworks — a solved layer

- **Laptop is enough** for daily and minute bars: 3000 US stocks × 20y
  of daily ≈ 15M rows; 10y of minutes ≈ 2.9B rows, which fits in
  64 GB with columnar chunking or vectorization. vectorbt's demo
  standard is million-parameter sweeps in tens of seconds; memory
  peaks are the only real bottleneck.
- **Extra compute is situational**: spot GPUs (~60–90% off; H100
  ≈$1.49/hr) only pay off for deep-model rolling training — a run
  costs $10–30. QuantConnect cloud nodes ~$0.14–1.15/hr if you'd
  rather not own the box.
- **Framework picks, maintenance-checked 2026-09**:

  | Stack | Status | Role |
  | --- | --- | --- |
  | freqtrade | active (54.6k★, 2026.8) | crypto full pipeline, production-usable incl. hyperopt + Telegram monitoring |
  | Nautilus Trader | active (Rust core) | event-driven, closest to institutional grade; steep curve |
  | vectorbt | active | research/vectored backtests, **no live execution** |
  | Hummingbot | active | market making specifically |
  | Qlib | release stalled 2025-08 | ML factor pipelines, research-oriented |
  | backtrader | last push 2024-08 — **de facto EOL** | do not start new projects on it |

- **Overfitting control as infrastructure**: purged/embargo CV and
  CPCV have almost no maintained open-source wheels (mlfinlab went
  closed-source; vectorbt PRO $240/yr bundles the statistics). The
  honest minimum for one person: 3-fold walk-forward + double-fee/
  double-slippage runs + ~20 lines of numpy for a deflated Sharpe.
  A realistic weekly cadence: 1–2 ideas, 10–20 focused hours, half of
  it spent on data cleaning and reruns.

## 4. Execution channel — where the exploration actually stops

This is the layer where a mainland-based solo operator hits walls that
no amount of clever code removes, and the walls were built *this year*
(2025–2026):

**US equities**

- **Futu / Tiger / Longbridge**: the eight-agency crackdown of 2026-05
  (fines: Futu ~¥1.85B, Tiger ~¥411M) ended it — from 2026-06-12
  mainland clients may only **sell and withdraw**; deposits and buys
  are closed; full wind-down in two years (~HK$200–250B of legacy
  assets under remediation).
- **IBKR**: since 2025-09, mainland-ID applicants need proof of
  overseas residence/work (or existing overseas assets) — pure
  mainland-resident onboarding is effectively closed.
- **Alpaca**: not available to mainland residents; note also its free
  tier routes flow PFOF-style (Elite DASH DMA at $0.004/share is the
  algo-usable tier), and FINRA's best-execution review ("FINRA
  Forward", 2026-07) makes PFOF economics unsettled.
- One genuine improvement: **the $25k PDT rule was abolished
  2026-06-04** (risk-based day-trading margin, ~$2k floor) — the
  institutional barrier for small intraday accounts dropped, *if* you
  can hold an account at all.
- Cost structure once inside: commissions ≈ 0; the real costs are
  spread+impact (~2–6 bp round-trip for S&P names, 20–80 bp for
  Russell-2000/microcaps), SEC §31 sell fee $20.60/million (FY2026),
  and borrow for shorts — hard-to-borrow at 5–100%+/yr with recall
  risk eats most small-cap short alpha.

**Crypto**

- 2021's classification of virtual-currency business as "illegal
  financial activity" stands and tightened; offshore venues are banned
  from serving mainland residents "in any form". Binance/Bybit/OKX
  enforce KYC with CN restrictions (Bybit explicitly excludes, RMB
  pairs delisted). **Holding is not per-se criminalized; the legal
  risk concentrates in the off-ramp** — card freezes and
  "aiding-information-crimes" exposure via OTC/P2P flows.
  Regulated alternatives (HK VATP — 16 SFC-licensed venues, need HK
  presence; Singapore MPI; US venues) all require non-mainland status.
- Fee reality where accessible: spot ~0.10% taker, perps
  ~0.02%/0.05% maker/taker with VIP tiers; funding settles every 8h.

**Money movement**: the $50k/yr FX quota covers current-account items
only — securities purchases are not a permitted purpose, and banks
decline brokerage wires; post-2026-05 screening tightened further.
The exploration's blunt conclusion: **with a pure mainland identity and
pure RMB income, there is no compliant way to persistently fund an
offshore broker or exchange. Solve domicile and where the money
legally sits before solving alpha.**

## 5. Ops load — the part nobody demos

- Steady-state tax: **1–3 hours/week of pure feeding** — renames,
  delistings, re-download of missing months, adjustment-factor
  changes, exchange rate-limit edits.
- Verified solo failure modes: websocket dies silently while the
  process lives; partial fills unreconciled; UTC/DST skew; expired API
  tokens; dependency upgrades breaking adapters; full disks; unwatched
  weekends. The floor of defenses: heartbeat + Telegram alert, daily
  reconciliation against the exchange's own fills, read-only vs trade
  key separation, position caps hardcoded in code rather than config.
- Latency by frequency: daily/weekly rebalance needs nothing but cron
  on the laptop; 15-min/hourly bots run on a €10/mo VPS; cross-venue
  arb/market making needs sub-10 ms co-location near the venue
  (Binance→AWS Tokyo, Coinbase→us-east-1), ~$50–200/mo; true HFT is
  out of scope for a person.
- Where the repo's existing muscle transfers directly: the orchestrator
  pattern — deterministic harness, typed persisted runtime objects,
  budget-bounded state machine, LLM/model only proposes and the
  harness decides — *is* the shape a live trading bot needs. The
  golden-case/ replay discipline maps 1:1 onto strategy regression
  tests. This is the one genuine advantage in the whole survey.

## 6. Strategy niches — where the evidence still allows a person

Public-evidence verdicts on the usual candidates:

| Niche | State in 2025–26 | Solo verdict |
| --- | --- | --- |
| Funding-rate / basis carry | BitMEX 2017 saw ±1000%/yr; now compressed to ~±10%, and ETFs (Defiance NBIT/DETH) package it for retail | fine as an ops *training wheel*, not income at $10–100k |
| CEX triangular arb | Finance Research Letters 2025: 4,879 "opportunities", profit ≈ zero after fees | dead |
| CEX–DEX funding arb | Digital Finance 2026: results highly assumption-dependent (gas/slippage/liquidation) | not frictionless; risky |
| Open-source MM (Hummingbot) | ~40% strategy survival rate, vendor explicitly cannot audit user PnL; counterpart Wintermute prints $2.2B/day | only on long-tail pairs + liquidity-mining subsidies |
| US small-cap factors / PEAD | McLean–Pontiff: published anomalies decay 26% (US)–58% (global); PEAD squeezed by shorting convenience | thin; borrow costs bite |
| Weekly, uncrowded, sub-$M-capacity markets | institutions won't allocate there | **the one durable shape** — but absolute dollars capped by own capital |

## 7. What "good enough to earn" actually means

Stacking the sections: the honest entry requirement is
**$100k+ of losable capital + 3–5 years + a capacity-capped narrow
strategy, with counterparty and ops treated as the primary risk
managees** (FTX left an ~$8B client hole; the 2025-10-10 liquidation
cascade flushed $19B in a day with matching engines failing — delta-
neutral accounts got liquidated too). The opportunity cost line:
1–2 years × 2–3 evenings/night ≈ 1000+ hours ≈ $80k at contract rates,
usually more than year-one P&L.

A defensible lower-risk sequence, if pursued at all:

1. Resolve channel/domicile legality **first** (or accept the project
   dies at §4 and keep it as research).
2. Crypto side, $10–50k: funding-rate carry + a freqtrade-or-selfbuilt
   pipeline; the only KPI for year one is *a year of runs with no
   incident* — reconciliation clean, no silent-death, no manual
   rescues.
3. Reuse the harness architecture here; strategies come last.
4. Scale capital only after the ops record exists — the reverse of the
   usual order.

## Sources

Pricing/data: [Massive](https://massive.com/pricing) ·
[Alpaca data](https://alpaca.markets/data) ·
[EODHD](https://eodhd.com/pricing) · [Databento](https://databento.com/pricing) ·
[Norgate](https://norgatedata.com) · [IBKR market data](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php) ·
[data.binance.vision](https://data.binance.vision/) ·
[Tardis billing](https://docs.tardis.dev/faq/billing-and-subscriptions) ·
[CoinAPI](https://www.coinapi.io/products/market-data-api/pricing) ·
[Nautilus L2 tutorial (12 GB/day book)](https://nautilustrader.io/docs/latest/tutorials/backtest_orderbook_binance/)
Framework status: [vectorbt.dev](https://vectorbt.dev/) ·
[freqtrade](https://www.freqtrade.io/) · [hummingbot](https://hummingbot.org/) ·
[qlib](https://github.com/microsoft/qlib) ·
[autotradelab engine comparison](https://autotradelab.com/blog/backtrader-vs-nautilusttrader-vs-vectorbt-vs-zipline-reloaded) ·
[spot GPU pricing](https://introl.com/blog/spot-instances-preemptible-gpus-ai-cost-savings) ·
[VPS guidance](https://x-zoneservers.com/blog/vps-for-crypto-trading-bots)
Channel/regulation: [IBKR tightens mainland onboarding](https://www.yicaiglobal.com/news/interactive-brokers-tightens-rules-for-chinese-mainlanders-to-open-accounts) ·
[Xinhua: Tiger/Futu/Longbridge fines & wind-down](https://www.news.cn/legal/20260522/844f4dc5cbcb49df93e94b08cdad736d/c.html) ·
[King & Wood Malleson analysis](https://www.kingandwood.com/cn/zh/insights/latest-thinking/china-launches-illegal-cross-border-stock-trading-what-to-do-next.html) ·
[FINRA PDT abolition (Notice 26-10)](https://www.finra.org/rules-guidance/notices/26-10) ·
[SEC §31 FY2026](https://www.sec.gov/rules-regulations/fee-rate-advisories/2026-2) ·
[CMS crypto-regulation guide CN](https://cms.law/en/int/expert-guides/cms-expert-guide-to-crypto-regulation/china) ·
[SFC VATP list](https://www.sfc.hk/en/Welcome-to-the-Fintech-Contact-Point/Virtual-assets/Virtual-asset-trading-platforms-operators/Lists-of-virtual-asset-trading-platforms) ·
[HSBC personal FX guide](https://www.hsbc.com.cn/content/dam/hsbc/cn/docs/foreign-exchange/personal-foreign-exchange-business-handling-guide-en.pdf)
Profitability evidence: [Barber–Odean day-trading skill](https://faculty.haas.berkeley.edu/odean/papers/day%20traders/Day%20Trading%20Skill%20110523.pdf) ·
[CFTC retail futures study](https://www.cftc.gov/sites/default/files/2024-11/Retail_Traders_Futures_V2_new_ada.pdf) ·
[MIT Sloan options study](https://mitsloan.mit.edu/ideas-made-to-matter/retail-investors-lose-big-options-markets-research-shows) ·
[BitMEX derivatives report](https://www.bitmex.com/blog/2025q2-derivatives-report) ·
[BTC carry vs Treasuries](https://finance.yahoo.com/markets/crypto/articles/bitcoin-carry-trade-tops-treasury-172634089.html) ·
[triangular-arb study](https://ideas.repec.org/a/eee/finlet/v73y2025ics154461232401537x.html) ·
[Hummingbot community-MM survey](https://hummingbot.org/blog/does-community-based-market-making-work/) ·
[McLean–Pontiff anomaly decay](https://www.jstor.org/stable/43869094) ·
[Bailey–López de Prado PBO](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) ·
[St. Gallen crypto delisting bias](https://alexandria.unisg.ch/server/api/core/bitstreams/2bc8397d-47dd-4f66-8467-9004b2c9d212/content) ·
[CoinShares 2025-10-10 liquidations](https://coinshares.com/insights/knowledge/billions-in-liquidations-what-happened/)
