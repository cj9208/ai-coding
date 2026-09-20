"""Preference profile rules (04 §2): the model proposes, code validates.

Weights are numbers a human will audit, so the invariants (sum to 1, every
trace points at an answer/default) are enforced here, not requested in a
prompt.
"""

from __future__ import annotations

import logging

from ..contracts.models import Criterion, PreferenceProfile, ProfileDraft

logger = logging.getLogger(__name__)

MIN_WEIGHT = 0.02  # below this a criterion is rounding noise; drop it


def finalize_profile(
    draft: ProfileDraft, *, criteria_universe: set[str], session_id: str
) -> PreferenceProfile:
    kept: list[Criterion] = []
    for c in draft.criteria:
        if c.key not in criteria_universe:
            logger.warning("dropping unknown criterion %r (not in findings)", c.key)
            continue
        if c.weight >= MIN_WEIGHT:
            kept.append(c)
    if not kept:
        kept = [
            Criterion(
                key="overall_fit",
                weight=1.0,
                source="default (no valid criteria proposed)",
            )
        ]
    total = sum(c.weight for c in kept)
    for c in kept:  # renormalize: sum-to-1 is a contract, not a request
        c.weight = round(c.weight / total, 4)
    drift = 1.0 - sum(c.weight for c in kept)
    kept[-1].weight = round(kept[-1].weight + drift, 4)  # exact-sum bookkeeping

    return PreferenceProfile(
        session_id=session_id,
        hard_constraints=draft.hard_constraints,
        criteria=kept,
        taste_notes=draft.taste_notes,
    )


def default_profile(criteria_universe: set[str], session_id: str) -> PreferenceProfile:
    """No-LLM path: no answers AND no user context => equal weights over the
    most-cited criteria. 'LLM calls only where they change the answer.'"""
    if not criteria_universe:
        return PreferenceProfile(
            session_id=session_id,
            criteria=[
                Criterion(
                    key="overall_fit",
                    weight=1.0,
                    source="default (no evidence criteria)",
                )
            ],
        )
    top = sorted(criteria_universe)[:5]
    w = round(1.0 / len(top), 4)
    criteria = [
        Criterion(key=k, weight=w, source="default (no user input)") for k in top
    ]
    criteria[-1].weight = round(1.0 - w * (len(top) - 1), 4)
    return PreferenceProfile(session_id=session_id, criteria=criteria)
