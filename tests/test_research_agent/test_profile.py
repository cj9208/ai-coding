from __future__ import annotations

import pytest

from research_agent.contracts.models import ProfileDraft
from research_agent.recommendation.profile import default_profile, finalize_profile


class TestFinalizeProfile:
    def test_weights_renormalized_to_one(self):
        draft = ProfileDraft(
            criteria=[
                {"key": "price", "weight": 0.6, "from": "ques_1"},
                {"key": "battery", "weight": 0.6, "from": "default"},
            ]
        )
        p = finalize_profile(
            draft, criteria_universe={"price", "battery"}, session_id="s"
        )
        assert abs(sum(c.weight for c in p.criteria) - 1.0) < 1e-9
        assert p.criteria[0].weight == pytest.approx(0.5)

    def test_unknown_criteria_dropped(self):
        draft = ProfileDraft(
            criteria=[
                {"key": "price", "weight": 1, "from": "a"},
                {"key": "esports", "weight": 1, "from": "hallucinated"},
            ]
        )
        p = finalize_profile(draft, criteria_universe={"price"}, session_id="s")
        assert [c.key for c in p.criteria] == ["price"]
        assert p.criteria[0].weight == 1.0

    def test_empty_profile_gets_overall_fit(self):
        p = finalize_profile(
            ProfileDraft(criteria=[]), criteria_universe=set(), session_id="s"
        )
        assert p.criteria[0].key == "overall_fit"


class TestDefaultProfile:
    def test_equal_weights_sum_one(self):
        p = default_profile({"price", "battery", "reliability"}, "s")
        assert abs(sum(c.weight for c in p.criteria) - 1.0) < 1e-9
        assert all("default" in c.source for c in p.criteria)

    def test_no_criteria(self):
        p = default_profile(set(), "s")
        assert p.criteria[0].key == "overall_fit"
