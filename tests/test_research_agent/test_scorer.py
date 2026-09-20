from __future__ import annotations

from research_agent.contracts.models import (
    Assessment,
    Criterion,
    CriterionRating,
    EvidencedPoint,
    PreferenceProfile,
)
from research_agent.recommendation import scorer


def profile():
    return PreferenceProfile(
        criteria=[
            Criterion(key="price", weight=0.5),
            Criterion(key="battery", weight=0.5),
        ]
    )


class TestAudit:
    def test_fake_citations_stripped_and_rating_demoted(self):
        a = Assessment(
            candidate="X",
            for_points=[EvidencedPoint(text="cheap", evidence=["F1", "F999"])],
            ratings=[CriterionRating(criterion="price", score=0.9, evidence=["F999"])],
        )
        audited = scorer.audit_assessment(
            a, finding_ids={"F1"}, criterion_keys={"price"}
        )
        assert audited.for_points[0].evidence == ["F1"]
        assert audited.ratings[0].score is None  # uncited number -> abstain

    def test_real_citation_survives(self):
        a = Assessment(
            candidate="X",
            ratings=[CriterionRating(criterion="price", score=0.9, evidence=["F1"])],
        )
        assert scorer.audit_assessment(a, {"F1"}, {"price"}).ratings[0].score == 0.9


class TestRank:
    def test_ordering_and_no_evidence_split(self):
        rated = Assessment(
            candidate="X",
            ratings=[
                CriterionRating(criterion="price", score=0.9, evidence=["F1"]),
                CriterionRating(criterion="battery", score=0.5, evidence=["F2"]),
            ],
        )
        partial = Assessment(
            candidate="Y",
            ratings=[CriterionRating(criterion="price", score=0.99, evidence=["F1"])],
        )
        silent = Assessment(candidate="Z", ratings=[])
        ranked = scorer.rank(
            profile(), _draft([rated, partial, silent]), finding_ids={"F1", "F2"}
        )
        assert ranked.order[0].candidate == "X"  # full coverage wins
        assert "Z" in ranked.no_evidence
        assert ranked.coverage["Y"] == 0.5  # half the profile unrated

    def test_constraint_dropped_excluded(self):
        a = Assessment(
            candidate="X",
            ratings=[CriterionRating(criterion="price", score=1.0, evidence=["F1"])],
        )
        ranked = scorer.rank(profile(), _draft([a], dropped=["X"]), {"F1"})
        assert not ranked.order and ranked.dropped == ["X"]


def _draft(assessments, dropped=None):
    from research_agent.contracts.models import ScoringDraft

    return ScoringDraft(assessments=assessments, constraint_dropped=dropped or [])


class TestCandidates:
    def test_versions_trimmed_and_deduped(self):
        from research_agent.contracts.models import Finding

        f1 = Finding(
            id="F1",
            capture_id="C1",
            claim="a",
            quote="a",
            touches_candidates=["Model X 2024", "model x 2024 "],
        )
        f2 = Finding(
            id="F2",
            capture_id="C1",
            claim="b",
            quote="b",
            touches_candidates=["Model Y v3"],
        )
        cands = scorer.candidates_from_findings([f1, f2])
        assert "Model X" in cands and "Model Y" in cands
        assert len(cands) == 2

    def test_bilingual_brand_aliases_merge(self):
        # live-run defect: same product cited as "华为 FreeBuds" and
        # "HUAWEI FreeBuds 6" got ranked as two candidates.
        from research_agent.contracts.models import Finding

        f1 = Finding(
            id="F1",
            capture_id="C1",
            claim="a",
            quote="a",
            touches_candidates=[
                "华为 FreeBuds",
                "华为 FreeBuds",
                "HUAWEI FreeBuds 6",
                "华为耳机FreeBuds",
            ],
        )
        cands = scorer.candidates_from_findings([f1])
        assert len(cands) == 1
        assert cands[0] == "华为 FreeBuds"  # most frequent spelling wins

    def test_distinct_models_not_merged(self):
        from research_agent.contracts.models import Finding

        f1 = Finding(
            id="F1",
            capture_id="C1",
            claim="a",
            quote="a",
            touches_candidates=[
                "索尼 WF-1000XM5",
                "索尼 WH-1000XM4",
                "华为 FreeBuds 6",
            ],
        )
        assert len(scorer.candidates_from_findings([f1])) == 3
