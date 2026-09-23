"""Expected-heartbeat rules: silence *is* the report (§4.5).

A task never has to announce that it stopped — dispatch compares the ledger's
newest event per ``(project, kind)`` against ``config/notify/expectations.yaml``
and emits the alert itself. Evaluation is read-only and idempotent: while a
silence persists, one rule produces at most one alert per throttle window, so
a stream that stays dead for a day is a handful of ledger rows, not 288.

"Never seen at all" counts as silence. An enabled rule is a claim that the
heartbeat exists; the honest reading of no evidence is that the claim is
unmet, not that the rule should wait politely. This is why a rule is shipped
``enabled: false`` until its producer is actually scheduled.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .events import Event, Severity
from .policy import THROTTLE_WINDOW, parse_ts, within_window

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd])\s*$", re.IGNORECASE)


def parse_duration(raw: str) -> timedelta:
    """``"3h"`` / ``"90m"`` / ``"45s"`` / ``"2d"`` -> timedelta."""
    match = _DURATION.match(raw or "")
    if not match:
        raise ValueError(f"max_silence must be a number plus s/m/h/d (got {raw!r})")
    return timedelta(seconds=float(match.group(1)) * _UNITS[match.group(2).lower()])


@dataclass(frozen=True)
class Expectation:
    project: str
    kind: str
    max_silence: str
    message: str = ""
    severity: str = Severity.ALERT.value
    enabled: bool = True
    dedup_key: str | None = None

    @property
    def key(self) -> str:
        """Stable dedup_key for the alerts this rule produces."""
        return self.dedup_key or f"{self.project}:{self.kind}:silent"

    @property
    def window(self) -> timedelta:
        return parse_duration(self.max_silence)

    @property
    def silent_kind(self) -> str:
        return f"{self.kind}.silent"


def load(path: Path | str) -> list[Expectation]:
    """Read the expectations file; a missing file means no rules (not an
    error — notify is usable before anyone declares a heartbeat)."""
    file = Path(path)
    if not file.exists():
        return []
    import yaml  # deferred: same posture as orchestrator.registry

    try:
        raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{file}: {exc}") from exc
    entries = raw.get("expectations") or []
    rules = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ValueError(f"expectations[{index}] must be a mapping, got {entry!r}")
        missing = {"project", "kind", "max_silence"} - entry.keys()
        if missing:
            raise ValueError(f"expectations[{index}] missing {sorted(missing)}")
        rule = Expectation(
            project=str(entry["project"]),
            kind=str(entry["kind"]),
            max_silence=str(entry["max_silence"]),
            message=str(entry.get("message", "")),
            severity=str(entry.get("severity", Severity.ALERT.value)),
            enabled=bool(entry.get("enabled", True)),
            dedup_key=(
                str(entry["dedup_key"]) if entry.get("dedup_key") is not None else None
            ),
        )
        parse_duration(rule.max_silence)  # fail on the config, not at 3am
        try:
            Severity(rule.severity)
        except ValueError as exc:
            raise ValueError(f"expectations[{index}]: {exc}") from exc
        rules.append(rule)
    return rules


def evaluate(
    expectations: Sequence[Expectation],
    last_seen: Mapping[tuple[str, str], str],
    last_alert: Mapping[str, str] | None = None,
    *,
    now: datetime | None = None,
    min_interval: timedelta = THROTTLE_WINDOW,
) -> list[Event]:
    """The alerts this ledger owes right now, in rule order.

    ``last_seen`` is ``(project, kind) -> ts`` from the ledger; ``last_alert``
    is ``dedup_key -> ts`` of previously emitted silence alerts, which is what
    makes a long silence one alert per window instead of one per dispatch.
    """
    stamp = now or datetime.now(timezone.utc)
    alerts: list[Event] = []
    for rule in expectations:
        if not rule.enabled:
            continue
        if within_window((last_alert or {}).get(rule.key), stamp, min_interval):
            continue

        seen_raw = last_seen.get((rule.project, rule.kind))
        seen = parse_ts(seen_raw)
        if seen is not None and (stamp - seen) <= rule.window:
            continue

        alerts.append(
            Event.new(
                rule.project,
                rule.silent_kind,
                severity=rule.severity,
                dedup_key=rule.key,
                payload={
                    "message": rule.message,
                    "max_silence": rule.max_silence,
                    "last_ts": seen_raw,
                    "never_seen": seen is None,
                    "silent_seconds": (
                        None if seen is None else int((stamp - seen).total_seconds())
                    ),
                },
            )
        )
    return alerts
