"""Screening arithmetic is exact where it matters: the shift, the cost
charge, and the holdout clamp."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import polars as pl
import pytest

from quantdesk import screen


def test_decision_earns_from_the_next_bar_and_costs_once(
    synth: SimpleNamespace,
) -> None:
    wide = synth.make_wide(20, {"WINNERUSDT": 0.01})
    days = wide["date"].to_list()
    frame = screen.pnl(wide, {days[4]: {"WINNERUSDT": 1.0}}, cost_bp=10.0)
    first = frame.filter(pl.col("date") == days[5])
    assert first["turnover"][0] == pytest.approx(1.0)  # we bought 100%
    assert first["cost"][0] == pytest.approx(10.0 * screen.BP)
    assert first["gross"][0] == pytest.approx(0.01)
    # exactly one cost day, however long we hold
    assert frame["cost"].sum() == pytest.approx(10.0 * screen.BP)
    assert frame["date"].min() == days[5]  # warm-up days are not statistics


def test_holdout_is_clamped_structurally(synth: SimpleNamespace) -> None:
    wide = synth.make_wide(1000, {"WINNERUSDT": 0.001})
    last = cast(date, wide["date"].max())
    spec = screen.ScreenSpec(
        factor="csm",
        params={"lookback": 120, "skip": 5, "hold": 1},
        dataset="um_klines_1d",
        symbols=["WINNERUSDT"],
        rebalance_every=5,
        start=synth.START.isoformat(),
        end=last.isoformat(),
    )
    result = screen.run_screen(spec, wide)
    sealed = last - timedelta(days=screen.HOLDOUT_DAYS)
    assert result.sealed_from == sealed.isoformat()
    m = result.metrics[10.0]
    assert m["days"] > 0 and m["to"] <= sealed.isoformat()


def test_same_inputs_give_a_byte_identical_manifest(
    synth: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(screen, "RUNS_DIR", tmp_path / "runs")
    wide = synth.make_wide(600, {"WINNERUSDT": 0.001, "LOSERUSDT": -0.001})
    last = cast(date, wide["date"].max())
    spec = screen.ScreenSpec(
        factor="csm",
        params={"lookback": 120, "skip": 5, "hold": 1},
        dataset="um_klines_1d",
        symbols=["WINNERUSDT", "LOSERUSDT"],
        rebalance_every=5,
        start=(synth.START + timedelta(days=300)).isoformat(),
        end=last.isoformat(),
    )
    paths = []
    for _ in range(2):
        result = screen.run_screen(spec, wide)
        paths.append(screen.record_run(result, ledger_path=tmp_path / "ledger.csv"))
    first, second = (p.read_text(encoding="utf-8") for p in paths)
    assert first == second  # re-runs are acceptance, not decoration
    ledger = (tmp_path / "ledger.csv").read_text(encoding="utf-8")
    assert ledger.count("\n") == 5  # header + one row per cost level, twice


def test_cli_screen_end_to_end(
    qdesk: Path,
    synth: SimpleNamespace,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from click.testing import CliRunner

    from quantdesk import cli as cli_mod
    from quantdesk.config import DATASETS

    wide = synth.make_wide(700, {"WINNERUSDT": 0.002, "LOSERUSDT": -0.001})
    slug = DATASETS["um_klines_1d"].dir_slug
    for symbol, column in (("WINNERUSDT", "WINNERUSDT"), ("LOSERUSDT", "LOSERUSDT")):
        leaf = qdesk / "parquet" / slug / f"symbol={symbol}" / "year=2022" / "month=01"
        leaf.mkdir(parents=True, exist_ok=True)
        frame = wide.select(
            pl.col("date")
            .cast(pl.Datetime("us"))
            .dt.replace_time_zone("UTC")
            .alias("open_time"),
            pl.col(column).alias("close"),
        )
        frame.write_parquet(leaf / "part-2022-01.parquet")
    monkeypatch.setattr(screen, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(screen, "LEDGER_FILE", tmp_path / "ledger.csv")
    result = CliRunner().invoke(
        cli_mod.cli,
        [
            "screen",
            "--factor",
            "csm",
            "--symbols",
            "WINNERUSDT,LOSERUSDT",
            "--since",
            (synth.START + timedelta(days=300)).isoformat(),
            "--until",
            "2100-01-01",
            "--rebalance",
            "5",
            "--set",
            "lookback=120",
            "--set",
            "skip=5",
            "--set",
            "hold=1",
        ],
    )
    assert result.exit_code == 0, result.output + repr(result.exception)
    out = result.output
    assert "stressed-cost verdict" in out and "sealed before" in out
    assert (tmp_path / "ledger.csv").is_file()
    assert list((tmp_path / "runs").glob("csm-*.json"))
