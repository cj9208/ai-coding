"""rules: silence is a gap, and a long silence must speak once per window (§4.5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from notify import rules
from notify.config import expectations_path
from notify.events import Severity

NOW = datetime(2026, 9, 23, 12, 0, 0, tzinfo=timezone.utc)


def _rule(**overrides: object) -> rules.Expectation:
    base: dict[str, object] = {
        "project": "quantdesk",
        "kind": "record.batch",
        "max_silence": "3h",
        "message": "no batches",
    }
    base.update(overrides)
    return rules.Expectation(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("45s", 45),
        ("90m", 5400),
        ("3h", 10800),
        ("2d", 172800),
        (" 6 H ".lower(), 21600),
    ],
)
def test_parse_duration_accepts_the_documented_units(raw: str, seconds: int) -> None:
    assert rules.parse_duration(raw) == timedelta(seconds=seconds)


@pytest.mark.parametrize("raw", ["3", "hours", "3x", "", "1h30m"])
def test_parse_duration_rejects_ambiguity(raw: str) -> None:
    with pytest.raises(ValueError):
        rules.parse_duration(raw)


def test_evaluate_alerts_once_past_max_silence() -> None:
    (event,) = rules.evaluate(
        [_rule()],
        {("quantdesk", "record.batch"): "2026-09-23T08:00:00+00:00"},
        now=NOW,
    )
    assert (event.project, event.kind, event.severity) == (
        "quantdesk",
        "record.batch.silent",
        Severity.ALERT,
    )
    assert event.dedup_key == "quantdesk:record.batch:silent"
    assert event.payload["silent_seconds"] == 14400
    assert event.payload["never_seen"] is False
    assert event.payload["message"] == "no batches"


def test_heartbeat_inside_the_window_is_silent() -> None:
    assert (
        rules.evaluate(
            [_rule()],
            {("quantdesk", "record.batch"): "2026-09-23T10:00:00+00:00"},
            now=NOW,
        )
        == []
    )


def test_never_seen_counts_as_silence() -> None:
    # an enabled rule is a claim the heartbeat exists; no evidence fails it —
    # which is why the shipped file keeps its rule disabled until M2 wires it
    (event,) = rules.evaluate([_rule()], {}, now=NOW)
    assert event.payload["never_seen"] is True
    assert event.payload["last_ts"] is None


def test_disabled_rule_never_fires() -> None:
    assert rules.evaluate([_rule(enabled=False)], {}, now=NOW) == []


def test_a_persistent_silence_alerts_once_per_min_interval() -> None:
    alerts = rules.evaluate(
        [_rule()],
        {},
        {"quantdesk:record.batch:silent": "2026-09-23T11:30:00+00:00"},
        now=NOW,
    )
    assert alerts == []  # 30 min ago: inside the throttle window, stay quiet

    later = rules.evaluate(
        [_rule()],
        {},
        {"quantdesk:record.batch:silent": "2026-09-23T10:30:00+00:00"},
        now=NOW,
    )
    assert len(later) == 1  # past the window: the silence may speak again


def test_warn_severity_is_honoured() -> None:
    (event,) = rules.evaluate([_rule(severity="warn")], {}, now=NOW)
    assert event.severity is Severity.WARN


def test_load_missing_file_is_no_rules(tmp_path: Path) -> None:
    assert rules.load(tmp_path / "absent.yaml") == []


def test_load_reads_the_file_shape(tmp_path: Path) -> None:
    path = tmp_path / "expectations.yaml"
    path.write_text(
        "expectations:\n"
        "  - project: quantdesk\n"
        "    kind: record.batch\n"
        "    max_silence: 3h\n"
        "    enabled: false\n"
        "  - project: orchestrator\n"
        "    kind: run.turn\n"
        "    max_silence: 30m\n",
        encoding="utf-8",
    )
    loaded = rules.load(path)
    assert [(r.project, r.kind, r.enabled) for r in loaded] == [
        ("quantdesk", "record.batch", False),
        ("orchestrator", "run.turn", True),
    ]
    assert loaded[1].key == "orchestrator:run.turn:silent"


@pytest.mark.parametrize(
    "body",
    [
        "expectations:\n  - project: quantdesk\n    kind: record.batch\n",  # no max_silence
        "expectations:\n  - project: quantdesk\n    kind: k\n    max_silence: soon\n",
        "expectations:\n  - project: q\n    kind: k\n    max_silence: 3h\n    severity: critical\n",
        "expectations:\n  - just a string\n",
        "expectations:\n  oops: [unclosed\n",
    ],
)
def test_load_fails_on_the_config_not_at_3am(tmp_path: Path, body: str) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError):
        rules.load(path)


def test_shipped_expectations_file_loads_and_is_quiet() -> None:
    # The tracked file is what a scheduled dispatch reads. Until the recorder is
    # actually scheduled (M2, §4.8), every rule in it must stay disabled — an
    # enabled rule with no producer is a phone that rings on a true statement.
    loaded = rules.load(expectations_path())
    assert loaded, "the tracked expectations file should declare at least one rule"
    assert all(rule.enabled is False for rule in loaded)
