"""Reflector (02 §4): loop control over a DIGEST, never the raw pages.

Deciding "am I done" needs a summary view; feeding it 40 full pages wastes
the very budget it guards. Code owns the part the LLM must not fake:
novelty counting (02 §4 stop condition 1: two consecutive low-novelty
batches saturate) and the gap-id namespace (pg*/eg* never reused).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..contracts.models import (
    Finding,
    GapDraft,
    ReflectDecision,
    ResearchBrief,
    SourceFailure,
)
from ..contracts.prompts import render_prompt

logger = logging.getLogger(__name__)

DIGEST_MAX_FINDINGS = 80
LOW_NOVELTY_FLOOR = 0.2  # <20% of a batch's claims are new clusters => low
SATURATION_STREAK = 2  # need TWO consecutive low-novelty batches


@dataclass
class Novelty:
    new_clusters: int
    total: int

    @property
    def low(self) -> bool:
        return self.total > 0 and self.new_clusters / self.total < LOW_NOVELTY_FLOOR


def batch_novelty(all_findings: list[Finding], batch_no: int) -> Novelty:
    seen_before: set[str] = set()
    new = total = 0
    for f in all_findings:
        if f.batch < batch_no:
            seen_before.add(f.support_key)
        elif f.batch == batch_no:
            total += 1
            if f.support_key not in seen_before:
                new += 1
                seen_before.add(f.support_key)
    return Novelty(new_clusters=new, total=total)


class Reflector:
    def __init__(self, llm):
        self.llm = llm

    async def decide(
        self,
        brief: ResearchBrief,
        *,
        intents: list[str],
        findings: list[Finding],
        failures: list[SourceFailure],
        prev_evidence_gaps: list[GapDraft],
        prev_preference_gaps: list[GapDraft],
        batch_histories: list[str],
        remaining_calls: int,
        max_gap_id: int,
    ) -> ReflectDecision:
        prompt = render_prompt(
            "reflector",
            TOPIC=brief.topic,
            INTENTS=", ".join(intents) or "(none)",
            BATCH_SUMMARY="; ".join(batch_histories) or "(first batch)",
            FINDING_DIGEST=self._digest(findings),
            EVIDENCE_GAPS=_gaps_text(prev_evidence_gaps),
            PREFERENCE_GAPS=_gaps_text(prev_preference_gaps),
            SOURCE_FAILURES="; ".join(
                f"{f.query_id}:{f.adapter}:{f.error[:120]}" for f in failures
            )
            or "(none)",
            REMAINING_CALLS=str(remaining_calls),
        )
        decision = await self.llm.chat_json(
            prompt, schema=ReflectDecision, temperature=0.2
        )
        # code owns gap identity + routing: the LIST the model wrote to is the
        # routing decision (02 §5). A same-description gap is the SAME gap —
        # ids persist across iterations so asked/closed tracking works.
        known = {
            g.description.strip().casefold(): g.id
            for g in list(prev_evidence_gaps) + list(prev_preference_gaps)
            if g.id
        }
        n = max_gap_id
        for g in decision.evidence_gaps:
            key = g.description.strip().casefold()
            if key in known:
                g.id = known[key]
            else:
                n += 1
                g.id = f"eg{n}"
                known[key] = g.id
        for g in decision.preference_gaps:
            key = g.description.strip().casefold()
            if key in known:
                g.id = known[key]
            else:
                n += 1
                g.id = f"pg{n}"
                known[key] = g.id
        return decision

    @staticmethod
    def _digest(findings: list[Finding]) -> str:
        lines = []
        for f in findings[:DIGEST_MAX_FINDINGS]:
            cand = ",".join(f.touches_candidates) or "-"
            lines.append(
                f"{f.id} | {f.claim[:160]} | {f.kind} | {cand} | "
                f"{','.join(f.decision_criteria) or '-'}"
            )
        if len(findings) > DIGEST_MAX_FINDINGS:
            lines.append(f"(+{len(findings) - DIGEST_MAX_FINDINGS} more)")
        return "\n".join(lines) or "(no findings yet)"


def _gaps_text(gaps: list) -> str:
    return (
        "; ".join(
            f"{g.id}: {g.description[:140]} "
            f"[blocks: {','.join(g.blocked_criteria) or '-'}]"
            for g in gaps
        )
        or "(none)"
    )
