"""The input safety gate's machinery — row shape plus the first-match
engine (module map: "input gate: deterministic pattern table -> allow |
constrain | clarify_scope | refuse | handoff").

Since 05b step 1 the *tables* are locale-scoped data in
``orchestrator.packs`` (one pack = safety table + alias table + prompt
templates); callers keep using ``safety.evaluate(text, locale)``.

Position in the architecture: this is harness, not model. The gate runs
on ``original_input.text`` *before* any LLM call, so a refused request
never costs a token (and refuse/handoff short-circuit in ``interpret.py``).
The verdict lands on ``FrontHalfOutput`` and the routing table consumes
it via ``assess.confidence_of`` (refuse -> unsafe, handoff -> blocked,
clarify_scope -> ambiguous) — no other module interprets safety.

Same rows-as-data posture as ``policy.py`` (DP-7): every row carries an
id so tests and replay can name what fired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import ActionType, RiskLevel, SafetyDecision


@dataclass(frozen=True)
class SafetyRow:
    row_id: str
    pattern: re.Pattern[str]
    decision: SafetyDecision
    action_type: ActionType = ActionType.read_only
    risk: RiskLevel = RiskLevel.low
    reason: str = ""
    question: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SafetyVerdict:
    decision: SafetyDecision
    action_type: ActionType
    risk: RiskLevel
    reason: str
    row_id: str
    question: str = ""
    constraints: dict[str, Any] = field(default_factory=dict)


def first_match(text: str, table: tuple[SafetyRow, ...]) -> SafetyVerdict:
    """First fully matching row wins; every pack's table ends in an allow
    default, so a verdict always exists."""
    for row in table:
        if row.pattern.search(text):
            return SafetyVerdict(
                decision=row.decision,
                action_type=row.action_type,
                risk=row.risk,
                reason=row.reason,
                row_id=row.row_id,
                question=row.question,
                constraints=dict(row.constraints),
            )
    raise AssertionError("safety table is missing its allow default row")


def evaluate(text: str, locale: str = "zh") -> SafetyVerdict:
    """The gate callers use: resolve the locale's pack, then first-match
    its table. No pack -> the conservative ``s_unsupported_locale``
    verdict, never a silent allow.

    Late import: ``packs`` builds its tables from the types defined in
    this module, so the registry cannot be imported at module level.
    """
    from .packs import UNSUPPORTED_VERDICT, get_pack

    pack = get_pack(locale)
    if pack is None:
        return UNSUPPORTED_VERDICT
    return first_match(text, pack.safety_table)
