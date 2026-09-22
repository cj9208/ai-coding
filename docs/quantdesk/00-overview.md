# Quantdesk — Overview

**One sentence:** `src/quantdesk` is the research data plane and factor
screening harness for the personal-quant bootstrap plan — an append-only,
checksum-inventoried Parquet hive over the Binance public archive, a
recorder for the three feeds no archive carries, and a factor/screener
pair built so that *looking at tomorrow* and *forgetting the trial* are
both structurally impossible.

```text
why this is worth building at all     what the plan promised     how to run it
docs/personal-quant-trading-          docs/personal-quant-       docs/quantdesk/03-usage
exploration.md                        bootstrap-plan.md          (full quant CLI reference,
(capital arithmetic, channel walls,   (M0-M3 milestones,         backfill recipes,
cost layers — the *why* of the        D-1/D-2/D-3 decisions,     add-a-dataset /
whole endeavor)                       factor candidate table)    add-a-factor recipes)
        │                                     │
        └──────────┐   ┌──────────────────────┘
                   ▼   ▼
        why the CODE looks like this
        docs/quantdesk/01-design-rationale.md
        (inventory-and-replacement posture, the
         silence-is-a-gap rule, as-of purity, the
         stressed-cost verdict, holdout clamping)
                   │
                   ▼
        what actually shipped
        docs/quantdesk/02-implementation.md
        (9 modules, every tuned constant, the
         absorbed format traps, deviations)
```

## The document set

| File | Answers | Read it when |
| --- | --- | --- |
| `../personal-quant-trading-exploration.md` | Whether solo quant is viable at all: capital arithmetic, data/compute/execution cost layers, the 2025-26 mainland channel wall | Before spending any money — this is the *why*, and the wall is the binding constraint |
| `../personal-quant-bootstrap-plan.md` | The M0–M3 build order, factor candidates ranked by evidence × solo-accessible frequency, the validation gates (pre-registration, trial ledger, sealed holdout, MinTRL honesty) | Before adding scope — the plan is the decisions authority; do not re-derive it here |
| `01-design-rationale.md` | Why the data plane inventories checksums, why the recorder treats silence as a gap, why factors filter `as_of` as their first statement, why the holdout clamp lives in code | You want to change an architecture decision and need the load-bearing argument |
| `02-implementation.md` | The nine modules of `src/quantdesk`, every tuned constant, the Binance format traps already absorbed, where shipped code deviates from plan text | You are reading or modifying `src/quantdesk` |
| `03-usage.md` | Every `quant` subcommand and flag, the live-proof recipes already executed, how to add a dataset or a factor, how to read the trial ledger | You want to download, verify, record, or screen |

Start with the plan (`../personal-quant-bootstrap-plan.md`) if you have
never seen the endeavor; start with `03-usage.md` if you just want to run
it; `02` is the bridge — it also records the deviations, so an agent
trusting the plan alone will not be surprised.

## Status (2026-09-22)

- **M0 shipped and live-verified**: `download / convert / list / verify /
  universe` — BTCUSDT um 1m + funding round-tripped through the whole
  plane; `verify --remote` clean; September 2026 assembled from daily
  files with the unpublished tail correctly reported as `missing`.
- **M0.5 recorder shipped**: funding and open interest recorded live;
  the liquidations WS is implemented and silence-watched, but this
  machine's network path to `fstream.binance.com` answers pings and
  pushes zero frames — the positive path awaits another network
  (TODO.md).
- **M1 code shipped**: three pure as-of factors (CSM "avoid losers", TSM
  + vol targeting, extreme funding reversal), the screening harness with
  baseline/stressed costs, the structural 12-month holdout clamp,
  manifests + trial ledger in `config/quantdesk/`. 28 tests green.
- **M1 runs landed 2026-09-22 — all three fail the stressed gate.**
  Universe: **20 crypto-native majors** (first 20 of today's USD-M
  quote-volume ranking with tokenized stock/commodity perps skipped —
  the owner's call: large caps only, no short-history bets), daily
  bars + funding 2022-01-01..2026-06-30, holdout sealed before
  2025-08-31 and never read. Stressed Sharpe: csm (hold 5) 0.36,
  tsm 0.18, funding reversal −0.45 — none clears 0.5. The acceptance
  property held: the re-run rewrote a byte-identical manifest and the
  ledger records every run. This is M1 doing its job — cheap, honest
  negatives before any capital thinking; next move belongs to M2's
  protocol, not to parameter archaeology on these three.
- **M2 (validation protocol) and M3 (own event kernel)**: not started;
  their gates are fixed in the plan and unchanged by anything here.

## Data at a glance

Everything lives under `data/quantdesk/` (REPO_ROOT-anchored;
`QUANTDESK_DATA_DIR` overrides):

| Path | What |
| --- | --- |
| `raw/data/<market>/…/<name>.zip` | byte-identical upstream archives, mirror of the archive layout |
| `raw/inventory.jsonl` | one line per fetched file: sha256 *as observed*, remote checksum, replacement events — last-write-wins |
| `parquet/<dataset>/symbol=…/year=…/month=…/part-<YYYY-MM>.parquet` | the hive — the single source of truth research reads |
| `recorded/<stream>/day=…/part-HHMMSS.<ns>.parquet` | the private dataset the recorder accumulates (funding/OI/liquidations) |
| `recorded/gaps.log` | every dropout **and every alive-but-silent stream**, append-only |
| `universe/<market>-<day>.json` | dated symbol-list snapshots — the as-of raw material |
| `universe/rank-um-top100-<day>.csv`, `universe/screen-universe-um-top20-majors-<day>.csv` | the 2026-09-22 volume ranking and the frozen screening universe it produced (working copies; the audit truth is the `spec.symbols` list committed in every manifest) |

And outside `data/`, because they are evidence rather than data:
`config/quantdesk/trial_ledger.csv` (one row per variant ever evaluated,
failures included) and `config/quantdesk/runs/<run_id>.json` (per-run
manifests) — both tracked.
