"""Deterministic conditioning of the raw input (design DP-2: Chinese is
first-class, and the alias/short-name table is where that earns the most —
cheap and auditable).

Contract with the rest of the front half:
- ``original_input.text`` is never rewritten or translated (input
  preservation, CH01 Stage 1); this module only *derives* a normalized
  query, and every derivation is traceable via ``rule_hits``.
- the returned scores feed ``DeterministicSignals`` and thence
  ``assess.confidence_of`` — a strong alias hit is positive evidence the
  routing table can trust without any model involvement.

``ALIASES`` is data-as-code (DP-7 posture): canonical name -> known
short/alternative names, longest match wins. Promotion to a config file is
not planned before M2's registry YAML.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

#: canonical entity name -> alias spellings (Chinese nicknames, short names,
#: mixed-language forms). Sample enterprise vocabulary; extend by row.
ALIASES: dict[str, tuple[str, ...]] = {
    "春晖省钱卡": ("春晖卡", "省钱卡", "春晖省钱卡"),
    "费用报销系统": ("报销系统", "报销平台", "费用报销", "报销"),
    "人事服务": ("HR服务", "hr服务", "人事系统"),
    "差旅标准": ("出差标准", "差旅政策"),
}

_COLLAPSE = re.compile(r"\s+")


@dataclass
class Normalization:
    """Output of one conditioning pass, mappable onto DeterministicSignals."""

    normalized_query: str
    alias_hits: list[str] = field(default_factory=list)  # canonical names
    rule_hits: list[str] = field(default_factory=list)  # traceable derivations
    top_match_score: float | None = None
    top2_gap: float | None = None
    candidate_count: int = 0

    def as_signals(self) -> dict[str, Any]:
        return {
            "alias_hits": list(self.alias_hits),
            "rule_hits": list(self.rule_hits),
            "top_match_score": self.top_match_score,
            "top2_gap": self.top2_gap,
            "candidate_count": self.candidate_count,
        }


def normalize(
    text: str, aliases: dict[str, tuple[str, ...]] | None = None
) -> Normalization:
    """NFC-fold, collapse whitespace, then apply the alias table.

    Scoring is specificity, not probability: a canonical's score is
    ``len(matched alias) / len(longest alias of that canonical)`` — typing
    the full name scores 1.0, a nickname less. ``top2_gap`` compares the two
    best distinct canonicals, which is what ``assess`` reads as
    "close_candidates" ambiguity (CH02_03).
    """
    table = aliases if aliases is not None else ALIASES
    query = _COLLAPSE.sub(" ", unicodedata.normalize("NFC", text)).strip()
    norm = Normalization(normalized_query=query)

    # longest alias first so "春晖省钱卡" wins over "省钱卡" in one text
    pairs = sorted(
        ((alias, canonical) for canonical, names in table.items() for alias in names),
        key=lambda p: -len(p[0]),
    )
    best: dict[str, int] = {}  # canonical -> matched alias length
    for alias, canonical in pairs:
        if alias in query and canonical not in best:
            best[canonical] = len(alias)
            norm.rule_hits.append(f"alias:{canonical}<-{alias}")
            norm.normalized_query = norm.normalized_query.replace(alias, canonical)

    if not best:
        return norm
    ranked = sorted(
        best.items(), key=lambda kv: (-kv[1] / _longest_alias(table[kv[0]]),)
    )
    scores = [
        min(1.0, length / _longest_alias(table[canonical]))
        for canonical, length in ranked
    ]
    norm.alias_hits = [canonical for canonical, _ in ranked]
    norm.candidate_count = len(ranked)
    norm.top_match_score = round(scores[0], 3)
    if len(scores) >= 2:
        norm.top2_gap = round(scores[0] - scores[1], 3)
    return norm


def _longest_alias(names: tuple[str, ...]) -> int:
    return max(len(n) for n in names)
