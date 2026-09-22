"""Factors are pure and future-blind — the as-of filter is tested as a
property of the function, not a hope."""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import polars as pl
import pytest

from quantdesk import signals


def test_csm_picks_the_strongest_and_is_future_blind(synth: SimpleNamespace) -> None:
    wide = synth.make_wide(400, {"WINNERUSDT": 0.002, "LOSERUSDT": -0.001})
    as_of = synth.START + timedelta(days=350)
    kwargs = {"lookback": 120, "skip": 5, "hold": 1}
    full = signals.cross_sectional_momentum(wide, as_of, **kwargs)
    assert full == {"WINNERUSDT": 1.0}
    # truncating every bar after as_of must not move the number: the
    # "tomorrow" rows (winner keeps running) are structurally invisible
    truncated = wide.filter(pl.col("date") <= as_of)
    again = signals.cross_sectional_momentum(truncated, as_of, **kwargs)
    assert again == full


def test_csm_skips_symbols_without_history(synth: SimpleNamespace) -> None:
    wide = synth.make_wide(400, {"OLDUSDT": 0.001})
    dates = wide["date"].to_list()
    new = [50.0 if d >= synth.START + timedelta(days=380) else None for d in dates]
    frame = wide.with_columns(pl.Series("NEWUSDT", new, pl.Float64))
    positions = signals.cross_sectional_momentum(
        frame, synth.START + timedelta(days=390), lookback=120, skip=5, hold=10
    )
    assert set(positions) == {"OLDUSDT"}  # 11 bars of history cannot score


def test_tsm_goes_with_the_trend_and_caps_gross(synth: SimpleNamespace) -> None:
    wide = synth.make_wide(400, {"UPUSDT": 0.002, "DOWNUSDT": -0.002})
    positions = signals.time_series_momentum(
        wide, synth.START + timedelta(days=390), lookback=90
    )
    assert positions["UPUSDT"] > 0 > positions["DOWNUSDT"]
    assert sum(abs(w) for w in positions.values()) <= 1.0 + 1e-12


def make_funding(start: date, symbol: str, rate_fn, days: int) -> pl.DataFrame:
    rows = []
    for d in range(days):
        for k in range(3):  # three settlements a day, like 8h funding
            calc = start + timedelta(days=d, hours=8 * k)
            rows.append(
                {"symbol": symbol, "calc_time": calc, "last_funding_rate": rate_fn(d)}
            )
    return pl.DataFrame(rows).with_columns(
        pl.col("calc_time").cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
    )


def test_funding_reversal_fades_the_tails(synth: SimpleNamespace) -> None:
    start = synth.START

    def spiky(d: int) -> float:
        return 0.001 if d < 55 else 0.05

    funding = pl.concat(
        [
            make_funding(start, "HOTUSDT", spiky, 60),
            make_funding(start, "COLDUSDT", lambda d: -spiky(d), 60),
        ],
        how="vertical",
    )
    # spike not yet visible: a flat tail sits mid-percentile, both sides
    quiet = signals.funding_reversal(
        funding, start + timedelta(days=55), window_days=30, extreme=0.9
    )
    assert quiet == {}
    loud = signals.funding_reversal(
        funding, start + timedelta(days=59), window_days=30, extreme=0.9
    )
    assert loud["HOTUSDT"] < 0 < loud["COLDUSDT"]
    assert sum(abs(w) for w in loud.values()) == pytest.approx(1.0)
