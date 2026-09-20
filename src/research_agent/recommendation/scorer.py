"""Scoring & ranking (04 §3): argument-first, citations audited, ties allowed.

The audit chain: every evidence id in an argument must exist among findings,
or it is stripped; a rating left with no surviving citation is demoted to
abstain (None). Numbers can only come from real quotes, because code throws
away the ones that are not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..contracts.models import (
    AdversarialDraft,
    Assessment,
    Finding,
    PreferenceProfile,
    ScoringDraft,
)
from ..contracts.prompts import render_prompt

logger = logging.getLogger(__name__)

TIE_MARGIN = 0.05  # within this weighted gap, ranking is a coin flip -> argue
MAX_CANDIDATES = 12


#: bilingual brand aliases — findings cite the same product as "华为 FreeBuds"
#: and "HUAWEI FreeBuds"; unifying them is pure code, no LLM call (04 §1 step 2).
_BRAND_CANON = {
    "华为": "huawei",
    "huawei": "huawei",
    "索尼": "sony",
    "sony": "sony",
    "小米": "xiaomi",
    "xiaomi": "xiaomi",
    "红米": "redmi",
    "redmi": "redmi",
    "苹果": "apple",
    "apple": "apple",
    "三星": "samsung",
    "samsung": "samsung",
    "bose": "bose",
    "jbl": "jbl",
    "森海塞尔": "sennheiser",
    "sennheiser": "sennheiser",
    "漫步者": "edifier",
    "edifier": "edifier",
    "万魔": "1more",
    "1more": "1more",
}
_CN_BRANDS = {cn: en for cn, en in _BRAND_CANON.items() if not cn.isascii()}
_GENERIC_TOKENS = {
    "earbuds",
    "earphone",
    "earphones",
    "headphone",
    "headphones",
    "headset",
    "tws",
    "anc",
}


def candidates_from_findings(findings: list[Finding]) -> list[str]:
    """Union of mentioned candidates, alias-normalized. The canonical key folds
    case, brand aliases, and generic noise; the display name is the most
    frequent original spelling. Versions are trimmed."""
    counts: dict[str, dict[str, int]] = {}
    for f in findings:
        for raw in f.touches_candidates:
            name = _normalize(raw)
            key = _key(name)
            if not key:
                continue
            counts.setdefault(key, {})
            counts[key][name] = counts[key].get(name, 0) + 1
    return [
        max(variants.items(), key=lambda kv: kv[1])[0] for variants in counts.values()
    ][:MAX_CANDIDATES]


def _normalize(raw: str) -> str:
    name = " ".join(raw.split())
    # strip trailing version noise: "Foo 2.4", "Foo v3"
    parts = name.split()
    while parts and (
        parts[-1][0:1].isdigit()
        or parts[-1].lower().startswith("v")
        and parts[-1][1:].isdigit()
    ):
        parts = parts[:-1]
    return " ".join(parts)


def _key(name: str) -> str:
    """Canonical identity for alias merging (display keeps the real spelling)."""
    text = name.lower().replace("耳机", " ")
    tokens: list[str] = []
    for t in text.split():
        if t in _GENERIC_TOKENS or not t.strip("-_0."):
            continue
        if t in _BRAND_CANON:
            tokens.append(_BRAND_CANON[t])
            continue
        # glued Chinese brand: "华为freebuds" -> huawei + freebuds
        for cn, en in _CN_BRANDS.items():
            if t.startswith(cn):
                tokens.append(en)
                rest = t[len(cn) :]
                if rest.strip("-_0."):
                    tokens.append(rest)
                break
        else:
            tokens.append(t)
    return " ".join(tokens)


def audit_assessment(
    a: Assessment, finding_ids: set[str], criterion_keys: set[str]
) -> Assessment:
    """Strip fake citations; demote uncited ratings to abstain."""
    for p in a.for_points + a.against_points:
        p.evidence = [e for e in p.evidence if e in finding_ids]
    for r in a.ratings:
        r.evidence = [e for e in r.evidence if e in finding_ids]
        if r.score is not None and not r.evidence:
            logger.info(
                "%s/%s: rating had no surviving citation -> abstain",
                a.candidate,
                r.criterion,
            )
            r.score = None
        if r.criterion not in criterion_keys:
            r.criterion = r.criterion  # keep; ranking ignores unknown keys
    return a


def weighted_scores(profile: PreferenceProfile, a: Assessment) -> tuple[float, float]:
    """(score, coverage). Abstentions contribute ZERO, not the mean of what
    is left: a candidate that dodges 80% of the weights must not be ranked
    on the flattering 20% it answered."""
    weights = {c.key: c.weight for c in profile.criteria}
    acc = covered = 0.0
    for r in a.ratings:
        w = weights.get(r.criterion)
        if w is None or r.score is None:
            continue
        acc += w * r.score
        covered += w
    if covered == 0:
        return 0.0, 0
    return acc, round(covered, 4)


@dataclass
class Ranked:
    order: list[Assessment] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)
    coverage: dict[str, float] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)  # constraint-dropped
    no_evidence: list[str] = field(default_factory=list)


def rank(
    profile: PreferenceProfile, draft: ScoringDraft, finding_ids: set[str]
) -> Ranked:
    known = {c.key for c in profile.criteria}
    assessments = [audit_assessment(a, finding_ids, known) for a in draft.assessments]
    scored: list[tuple[float, Assessment]] = []
    ranked = Ranked(dropped=list(draft.constraint_dropped))
    for a in assessments:
        if a.candidate in ranked.dropped:
            continue
        score, cov = weighted_scores(profile, a)
        if cov == 0:
            ranked.no_evidence.append(a.candidate)
            continue
        ranked.scores[a.candidate] = score
        ranked.coverage[a.candidate] = cov
        scored.append((score, a))
    scored.sort(key=lambda sa: (-sa[0], sa[1].candidate))
    ranked.order = [a for _, a in scored]
    return ranked


async def adversarial_check(
    llm,
    *,
    topic: str,
    profile: PreferenceProfile,
    first: Assessment,
    second: Assessment,
    findings: list[Finding],
    language: str = "Chinese",
) -> AdversarialDraft:
    prompt = render_prompt(
        "adversarial",
        LANGUAGE=language,
        TOPIC=topic,
        FIRST=first.candidate,
        SECOND=second.candidate,
        FINDING_DIGEST=_digest_for(findings, first, second),
        PROFILE=_profile_text(profile),
        TOP2=f"#1 {first.candidate}: {_reasons(first)}\n"
        f"#2 {second.candidate}: {_reasons(second)}",
    )
    return await llm.chat_json(prompt, schema=AdversarialDraft, temperature=0.5)


def _digest_for(findings: list[Finding], *assessments: Assessment) -> str:
    cited = {
        e
        for a in assessments
        for p in a.for_points + a.against_points + [r for r in a.ratings]
        for e in p.evidence
    }
    lines = [f"{f.id} | {f.claim[:160]}" for f in findings if f.id in cited]
    return "\n".join(lines) or "(no cited findings)"


def _reasons(a: Assessment) -> str:
    pos = "; ".join(p.text for p in a.for_points[:2])
    neg = "; ".join(p.text for p in a.against_points[:2])
    return f"for: {pos or '-'} / against: {neg or '-'}"


def _profile_text(profile: PreferenceProfile) -> str:
    crits = ", ".join(f"{c.key}(w={c.weight})" for c in profile.criteria)
    cons = "; ".join(f"{c.key} {c.value}" for c in profile.hard_constraints) or "-"
    return (
        f"criteria: {crits}\nhard constraints: {cons}\n"
        f"taste: {', '.join(profile.taste_notes) or '-'}"
    )


async def score_candidates(
    llm,
    *,
    topic: str,
    profile: PreferenceProfile,
    candidates: list[str],
    findings: list[Finding],
    language: str = "Chinese",
) -> ScoringDraft:
    digest = "\n".join(
        f"{f.id} | {f.claim[:200]} | {','.join(f.decision_criteria) or '-'}"
        for f in findings[:120]
    )
    prompt = render_prompt(
        "score",
        LANGUAGE=language,
        TOPIC=topic,
        PROFILE=_profile_text(profile),
        CANDIDATES=", ".join(candidates),
        FINDING_DIGEST=digest or "(no findings)",
    )
    return await llm.chat_json(prompt, schema=ScoringDraft, temperature=0.3)
