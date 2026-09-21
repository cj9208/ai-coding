"""Normalizer + dedup (02 §3): the anti-hallucination seam and the
independence counter.

Two jobs, both pure functions (testable without any I/O):
1. Quote audit — a Finding whose quote cannot be matched inside its Capture
   is REJECTED. This is what makes fabricated "evidence" detectable, so ids
   are only assigned after the audit passes.
2. Support clustering — three blogs copy-pasting one press release are ONE
   corroboration, not three. Near-duplicate claims share a support_key; the
   Reflector digests see distinct-source counts.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"[a-z0-9\u4e00-\u9fff]+")
_STOP = {
    "the",
    "a",
    "an",
    "is",
    "are",
    "was",
    "were",
    "to",
    "of",
    "in",
    "for",
    "on",
    "and",
    "or",
    "with",
    "that",
    "this",
    "it",
    "as",
    "at",
    "by",
}

# fuzzy floor: extraction LLMs legitimately re-wrap lines; below this it is a
# paraphrase dressed as a quote, which is exactly what we are hunting.
QUOTE_FLOOR = 0.85
JACCARD_DUP = 0.8


def normalize_text(s: str) -> str:
    return _WS.sub(" ", s).strip().casefold()


def quote_ok(quote: str, capture_text: str) -> bool:
    """True if `quote` is a verbatim (whitespace-insensitive) span of the capture."""
    q, t = normalize_text(quote), normalize_text(capture_text)
    if not q:
        return False
    if q in t:
        return True
    match = SequenceMatcher(None, q, t, autojunk=False).find_longest_match(
        0, len(q), 0, len(t)
    )
    return match.size / len(q) >= QUOTE_FLOOR


def claim_tokens(claim: str) -> frozenset[str]:
    return frozenset(
        tok
        for tok in _TOKEN.findall(claim.casefold())
        if tok not in _STOP and len(tok) > 1
    )


class SupportClusterer:
    """Greedy near-dup clustering over claim token sets (session-scoped)."""

    def __init__(self) -> None:
        self._clusters: dict[str, frozenset[str]] = {}
        self._seq = 0

    def key_for(self, claim: str) -> str:
        tokens = claim_tokens(claim)
        if not tokens:
            self._seq += 1
            key = f"sk{self._seq}"
            self._clusters[key] = tokens
            return key
        best_key, best_sim = "", 0.0
        for key, existing in self._clusters.items():
            if not existing:
                continue
            inter = len(tokens & existing)
            union = len(tokens | existing)
            sim = inter / union if union else 0.0
            if sim > best_sim:
                best_key, best_sim = key, sim
        if best_sim >= JACCARD_DUP:
            return best_key
        self._seq += 1
        key = f"sk{self._seq}"
        self._clusters[key] = tokens
        return key

    def restore_from(self, used: dict[str, frozenset[str]]) -> None:
        """Resume a session: seed clusters from persisted findings."""
        self._clusters.update(used)
        nums = [int(k[2:]) for k in used if re.fullmatch(r"sk\d+", k)]
        self._seq = max(nums, default=0)


def support_count(findings, support_key: str) -> int:
    """Distinct captures backing one cluster — the honest corroboration count."""
    return len({f.capture_id for f in findings if f.support_key == support_key})
