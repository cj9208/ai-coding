"""policy.plan — golden cases (§4.4 rules pinned as data, §5 M1 "golden 用例入库").

Each line of ``tests/golden/notify_policy.jsonl`` is a whole decision: the
pending batch, what the channel already sent, how many rounds each event has
failed, and the clock. The expected output is reduced to
``(reason, [event ids])`` per message plus the stuck list, because the point
is *which events become which message* — not wording, not timestamps.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from notify.events import Event, Severity
from notify.policy import merge_notice, plan

GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "notify_policy.jsonl"
NOW = datetime.fromisoformat("2026-09-23T10:00:00+00:00")


def _cases() -> list[dict[str, Any]]:
    lines = GOLDEN.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _event(raw: dict[str, Any]) -> Event:
    return Event(
        id=int(raw["id"]),
        ts=str(raw["ts"]),
        project=str(raw["project"]),
        kind=str(raw["kind"]),
        severity=Severity(str(raw["severity"])),
        dedup_key=str(raw["dedup_key"]),
        payload=dict(raw.get("payload") or {}),
    )


def _observed(pending: list[Event], case: dict[str, Any]) -> dict[str, Any]:
    result = plan(
        pending,
        last_sent={str(k): str(v) for k, v in (case.get("last_sent") or {}).items()},
        attempts={int(k): int(v) for k, v in (case.get("attempts") or {}).items()},
        now=datetime.fromisoformat(str(case["now"])),
    )
    return {
        "messages": [[d.reason, list(d.folded)] for d in result.deliveries],
        "stuck": [e.id for e in result.stuck],
    }


@pytest.mark.parametrize("case", _cases(), ids=[c["case"] for c in _cases()])
def test_policy_golden(case: dict[str, Any]) -> None:
    pending = [_event(raw) for raw in case["pending"]]
    assert _observed(pending, case) == case["expect"]


def _group(*payloads: dict[str, Any]) -> list[Event]:
    return [
        Event(
            id=index,
            ts=f"2026-09-23T10:00:0{index}+00:00",
            project="quantdesk",
            kind="ws.silent",
            severity=Severity.ALERT,
            dedup_key="quantdesk:ws.silent",
            payload=payload,
        )
        for index, payload in enumerate(payloads, start=1)
    ]


def test_repeat_notice_folds_every_event_id() -> None:
    # §4.4: one message speaks for the whole burst — dispatch marks all of
    # them sent off this single delivery, so the ids must survive.
    notice = merge_notice(_group({"n": 1}, {"n": 2}, {"n": 3}), NOW)
    assert notice.payload["event_ids"] == [1, 2, 3]
    assert notice.payload["repeats"] == 3


def test_merge_notice_carries_counts_not_prose() -> None:
    # §4.1: the payload stays machine-readable; wording is the renderer's job
    notice = merge_notice(_group({"n": 1}, {"n": 2}, {"n": 3}), NOW)
    assert notice.payload["first_ts"] == "2026-09-23T10:00:01+00:00"
    assert notice.payload["last_ts"] == "2026-09-23T10:00:03+00:00"
    assert notice.payload["detail"] == {"n": 3}
    assert notice.id == -1  # a notice has no ledger row of its own
    assert (notice.project, notice.kind, notice.severity) == (
        "quantdesk",
        "ws.silent",
        Severity.ALERT,
    )
