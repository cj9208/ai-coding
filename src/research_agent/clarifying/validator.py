"""Question validator (03 §2, step 4): pure code, no LLM.

The rules the design refuses to delegate to model goodwill:
- every question grounded in >=1 EXISTING finding id -> else dropped;
- no evidence-gap may leak into questions (routing law, 02 §5);
- 2-5 options, each either citing findings or explicitly the user's own;
- never re-ask a gap that already produced a question (dedupe by gap_id);
- hard cap at max_questions.

Returning dropped-question reasons is part of the contract: "which rules bit"
is eval data (03 §6 false-positive rate).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..contracts.models import Question


@dataclass
class ValidationReport:
    kept: list[Question] = field(default_factory=list)
    dropped: list[tuple[str, str]] = field(default_factory=list)  # (gap_id, reason)


def validate_questions(
    questions: list[Question],
    *,
    finding_ids: set[str],
    evidence_gap_ids: set[str],
    asked_gap_ids: set[str],
    max_questions: int,
    allowed_gap_ids: set[str] | None = None,
) -> ValidationReport:
    report = ValidationReport()
    seen_gaps: set[str] = set()
    for q in questions:
        reason = _check(
            q, finding_ids, evidence_gap_ids, asked_gap_ids, seen_gaps, allowed_gap_ids
        )
        if reason:
            report.dropped.append((q.gap_id, reason))
            continue
        seen_gaps.add(q.gap_id)
        if len(report.kept) < max_questions:
            report.kept.append(q)
        else:
            report.dropped.append((q.gap_id, "over max_questions cap"))
    return report


def _check(
    q: Question,
    finding_ids: set[str],
    evidence_gap_ids: set[str],
    asked_gap_ids: set[str],
    seen_gaps: set[str],
    allowed_gap_ids: set[str] | None,
) -> str:
    if allowed_gap_ids is not None and q.gap_id not in allowed_gap_ids:
        return f"gap_id {q.gap_id!r} was not among the offered preference gaps"
    if q.gap_id in asked_gap_ids or q.gap_id in seen_gaps:
        return "gap already asked in an earlier round"
    if q.gap_id in evidence_gap_ids:
        return f"evidence gap leaked into questions ({q.gap_id})"
    grounded = [f for f in q.grounded_in if f in finding_ids]
    if not grounded:
        return "no grounded_in references an existing finding"
    if not 2 <= len(q.options) <= 5:
        return f"{len(q.options)} options; need 2-5"
    if not q.text.strip() or not q.why_asking.strip():
        return "missing text or why_asking"
    # trim citations to real ids; keep options whose cite-list empties out as
    # "the user's own" (allowed), drop a question only if NO option survives
    for opt in q.options:
        opt.cites = [c for c in opt.cites if c in finding_ids]
    q.grounded_in = grounded
    return ""
