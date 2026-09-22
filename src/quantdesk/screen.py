"""The screening harness — where time moves forward, once.

The plan's shift convention lives here and nowhere else: positions
decided at bar t's close are executed at t+1's open and earn the t+1
return; turnover costs land on the execution day. Signals themselves are
imported pure functions (:mod:`quantdesk.signals`) — the same objects a
later event kernel will import, which is what makes the M3
decision-consistency test possible at all.

Costs default non-zero and every run reports two levels: baseline
(10 bp/side taker) and stressed (2×). A factor *passes* only at stressed
costs — and "passing" here is screening, not proof: nothing moves from
this harness toward M2 without the trial ledger having recorded every
variant that got here, including the failures.

Plan deviation, recorded per the ledger's own spirit: the plan named
vectorbt for screening. At the slow band's daily grid the whole
arithmetic is weighted returns minus turnover costs — some 40 lines that
must agree bar-for-bar with the M3 kernel later. Adopting a second
engine's cost semantics as an act of faith would add a thing to
understand, not remove one; vectorbt stays on the comparison checklist.

The last 12 months of the hive are sealed here structurally: `end` is
clamped to (last bar - HOLDOUT_DAYS) on every run, so screening cannot
burn the holdout M2 is allowed to read exactly once.
"""

from __future__ import annotations

import csv
import hashlib
import json
import subprocess  # nosec B404 - git is the point; called as an argv list
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, cast

import polars as pl

from utils.paths import REPO_ROOT

from . import signals
from .signals import DATE_COL, FACTORS

BP = 1e-4  # one basis point of notional, per side

#: the ledger and manifests are tracked evidence, so they live in the
#: repo (config/), not in gitignored data/.
LEDGER_DIR = REPO_ROOT / "config" / "quantdesk"
LEDGER_FILE = LEDGER_DIR / "trial_ledger.csv"
RUNS_DIR = LEDGER_DIR / "runs"

COST_LEVELS = (10.0, 20.0)  # baseline and stressed (2x), bp per side
HOLDOUT_DAYS = 365
PASS_SHARPE = 0.5  # the M2 gate's stressed-cost floor, used as a screen flag


def code_hash() -> str:
    """sha256 over every quantdesk source file — the manifest's claim
    about *which code* produced this number."""
    digest = hashlib.sha256()
    root = Path(__file__).parent
    for file in sorted(root.rglob("*.py")):
        digest.update(file.relative_to(root).as_posix().encode())
        digest.update(file.read_bytes())
    return digest.hexdigest()[:16]


def git_rev() -> str:
    try:
        out = subprocess.run(  # nosec B603 B607 - git by name, argv list
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=True,
        )
    except Exception:  # noqa: BLE001 - a missing git is not a screen failure
        return "unknown"
    return out.stdout.strip() or "unknown"


@dataclass
class ScreenSpec:
    """One screening run, fully described — the manifest is this plus
    results."""

    factor: str
    params: dict[str, Any]
    dataset: str
    symbols: list[str]
    rebalance_every: int
    start: str  # YYYY-MM-DD, first day a decision may be made
    end: str  # YYYY-MM-DD requested; clamped by the holdout seal
    cost_levels: tuple[float, ...] = COST_LEVELS
    note: str = ""

    @property
    def run_id(self) -> str:
        stamp = hashlib.sha256(
            json.dumps(
                {
                    "factor": self.factor,
                    "params": self.params,
                    "symbols": sorted(self.symbols),
                    "rebalance": self.rebalance_every,
                    "start": self.start,
                    "end": self.end,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()[:10]
        return f"{self.factor}-{self.start}_{self.end}-{stamp}"


@dataclass
class ScreenResult:
    spec: ScreenSpec
    sealed_from: str  # holdout boundary actually applied
    metrics: dict[float, dict[str, Any]] = field(default_factory=dict)

    @property
    def passed_stressed(self) -> bool:
        stressed = max(self.spec.cost_levels)
        got = self.metrics.get(stressed)
        return bool(got and got["sharpe"] >= PASS_SHARPE)


def daily_returns(wide: pl.DataFrame) -> dict[date, dict[str, float]]:
    """date -> symbol -> close-to-close return (missing bar = no return)."""
    columns = [c for c in wide.columns if c != DATE_COL]
    exprs = [(pl.col(c) / pl.col(c).shift(1) - 1.0).alias(c) for c in columns]
    rows = wide.select([DATE_COL, *exprs]).to_dicts()
    return {
        r[DATE_COL]: {c: (r[c] if r[c] is not None else 0.0) for c in columns}
        for r in rows
    }


def schedule(
    wide: pl.DataFrame,
    factor: Callable,
    start: date,
    end: date,
    rebalance_every: int,
    kwargs: dict[str, Any],
) -> dict[date, dict[str, float]]:
    """Target positions on every `rebalance_every`-th bar in [start, end].
    The factor sees only `wide` filtered to as-of — see signals."""
    grid = wide.filter((pl.col(DATE_COL) >= start) & (pl.col(DATE_COL) <= end))[
        DATE_COL
    ].to_list()
    return {
        day: factor(wide, day, **kwargs)
        for i, day in enumerate(grid)
        if i % rebalance_every == 0
    }


def pnl(
    wide: pl.DataFrame,
    rebalances: dict[date, dict[str, float]],
    cost_bp: float,
) -> pl.DataFrame:
    """Execution-correct daily P&L: a decision made at bar t's close is
    traded at t+1 (turnover cost that day) and earns from t+1 forward."""
    dates = wide[DATE_COL].to_list()
    rates = daily_returns(wide)
    held: dict[str, float] = {}
    rows: list[dict[str, Any]] = []
    for i, day in enumerate(dates):
        if i == 0:
            continue
        decision = rebalances.get(dates[i - 1])
        turnover = 0.0
        if decision is not None:
            symbols = set(decision) | set(held)
            turnover = sum(
                abs(decision.get(s, 0.0) - held.get(s, 0.0)) for s in symbols
            )
            held = decision
        if not rows and turnover == 0.0 and not held:
            continue  # nothing has been decided yet — skip the warm-up
        day_return = rates[day]
        gross = sum(w * day_return.get(s, 0.0) for s, w in held.items())
        cost = turnover * cost_bp * BP
        rows.append(
            {
                DATE_COL: day,
                "gross": gross,
                "cost": cost,
                "net": gross - cost,
                "turnover": turnover,
            }
        )
    return pl.DataFrame(rows)


def metrics(frame: pl.DataFrame) -> dict[str, Any]:
    """Annualized stats of a pnl frame's net series, plus turnover."""
    values = frame["net"].to_list() if frame.height else []
    n = len(values)
    if frame.height == 0:
        return {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "days": 0}
    window = {"from": str(frame[DATE_COL].min()), "to": str(frame[DATE_COL].max())}
    if n < 2:
        return {"sharpe": 0.0, "cagr": 0.0, "max_drawdown": 0.0, "days": n, **window}
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    std = var**0.5
    equity = peak = 1.0
    max_dd = 0.0
    for v in values:
        equity *= 1.0 + v
        peak = max(peak, equity)
        max_dd = min(max_dd, equity / peak - 1.0)
    turnover_pa = sum(frame["turnover"].to_list()) / (n / 252.0)
    return {
        "sharpe": (mean / std) * (252**0.5) if std > 0 else 0.0,
        "cagr": equity ** (252.0 / n) - 1.0 if equity > 0 else -1.0,
        "max_drawdown": max_dd,
        "turnover_pa": turnover_pa,
        "cost_drag_pa": sum(frame["cost"].to_list()) / (n / 252.0),
        "days": n,
        **window,
    }


def run_screen(
    spec: ScreenSpec,
    wide: pl.DataFrame,
    funding: pl.DataFrame | None = None,
) -> ScreenResult:
    """Screen once: schedule the factor, then P&L it at each cost level.
    `end` is clamped to the sealed boundary before anything is read."""
    last = cast(date, wide[DATE_COL].max())
    sealed = last - timedelta(days=HOLDOUT_DAYS)
    end = min(date.fromisoformat(spec.end), sealed)
    wide = wide.filter(pl.col(DATE_COL) <= end)  # nothing past the seal is read
    start = date.fromisoformat(spec.start)
    factor = FACTORS[spec.factor]
    kwargs = dict(spec.params)
    if spec.factor in signals.FAST_FACTORS:
        if funding is None:
            raise ValueError("funding factor needs the funding frame")
        kwargs["funding"] = funding
        position = _funding_schedule(factor, funding, wide, start, end, spec)
    else:
        position = schedule(wide, factor, start, end, spec.rebalance_every, kwargs)
    result = ScreenResult(spec=spec, sealed_from=sealed.isoformat())
    for cost in spec.cost_levels:
        result.metrics[cost] = metrics(pnl(wide, position, cost))
    return result


def _funding_schedule(factor, funding, wide, start, end, spec) -> dict:
    """The funding factor prices off funding rows, not closes; the
    rebalance grid still comes from the price calendar."""
    grid = wide.filter((pl.col(DATE_COL) >= start) & (pl.col(DATE_COL) <= end))[
        DATE_COL
    ].to_list()
    return {
        day: factor(funding, day, **spec.params)
        for i, day in enumerate(grid)
        if i % spec.rebalance_every == 0
    }


# ---------------------------------------------------------------- ledger


def manifest_payload(result: ScreenResult) -> dict[str, Any]:
    """Everything but the clock: the same inputs give a byte-identical
    manifest (run_at is added only to the ledger row)."""
    return {
        "run_id": result.spec.run_id,
        "code_hash": code_hash(),
        "git_rev": git_rev(),
        "spec": {
            "factor": result.spec.factor,
            "params": result.spec.params,
            "dataset": result.spec.dataset,
            "symbols": result.spec.symbols,
            "rebalance_every": result.spec.rebalance_every,
            "start": result.spec.start,
            "end": result.spec.end,
            "note": result.spec.note,
        },
        "sealed_from": result.sealed_from,
        "passed_stressed": result.passed_stressed,
        "metrics": {str(k): v for k, v in result.metrics.items()},
    }


def record_run(result: ScreenResult, ledger_path: Path | None = None) -> Path:
    """Append the trial(s) and write the manifest — the ledger first,
    because an unrecorded trial is the lie the plan forbids."""
    if ledger_path is None:
        ledger_path = LEDGER_FILE
    payload = manifest_payload(result)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = RUNS_DIR / f"{result.spec.run_id}.json"
    manifest.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    header = [
        "run_at",
        "run_id",
        "factor",
        "cost_bp",
        "sharpe",
        "cagr",
        "max_drawdown",
        "turnover_pa",
        "days",
        "passed_stressed",
        "params",
        "n_symbols",
        "data_start",
        "data_end",
        "sealed_from",
        "code_hash",
        "git_rev",
    ]
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    is_new = not ledger_path.is_file()
    with ledger_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        if is_new:
            writer.writeheader()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for cost, m in sorted(result.metrics.items()):
            writer.writerow(
                {
                    "run_at": now,
                    "run_id": result.spec.run_id,
                    "factor": result.spec.factor,
                    "cost_bp": cost,
                    "sharpe": f"{m['sharpe']:.4f}",
                    "cagr": f"{m.get('cagr', 0):.4f}",
                    "max_drawdown": f"{m.get('max_drawdown', 0):.4f}",
                    "turnover_pa": f"{m.get('turnover_pa', 0):.2f}",
                    "days": m["days"],
                    "passed_stressed": result.passed_stressed,
                    "params": json.dumps(result.spec.params, sort_keys=True),
                    "n_symbols": len(result.spec.symbols),
                    "data_start": result.spec.start,
                    "data_end": result.spec.end,
                    "sealed_from": result.sealed_from,
                    "code_hash": payload["code_hash"],
                    "git_rev": payload["git_rev"],
                }
            )
    return manifest
