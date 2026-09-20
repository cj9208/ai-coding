from __future__ import annotations

from research_agent.clarifying.validator import validate_questions
from research_agent.contracts.models import Option, Question


def q(gap_id="pg1", grounded=("F1",), options=2, why="because F1 says so", text="t?"):
    return Question(
        gap_id=gap_id,
        text=text,
        why_asking=why,
        grounded_in=list(grounded),
        options=[Option(value=f"v{i}", label=f"l{i}") for i in range(options)],
    )


class TestValidator:
    def test_keeps_well_formed_question(self):
        r = validate_questions(
            [q()],
            finding_ids={"F1"},
            evidence_gap_ids=set(),
            asked_gap_ids=set(),
            max_questions=4,
        )
        assert len(r.kept) == 1 and not r.dropped

    def test_drops_ungrounded(self):
        r = validate_questions(
            [q(grounded=("F999",))],
            finding_ids={"F1"},
            evidence_gap_ids=set(),
            asked_gap_ids=set(),
            max_questions=4,
        )
        assert not r.kept and "finding" in r.dropped[0][1]

    def test_drops_evidence_gap_leak(self):
        r = validate_questions(
            [q(gap_id="eg1")],
            finding_ids={"F1"},
            evidence_gap_ids={"eg1"},
            asked_gap_ids=set(),
            max_questions=4,
        )
        assert "leaked" in r.dropped[0][1]

    def test_never_re_asks_gap(self):
        r = validate_questions(
            [q(gap_id="pg7")],
            finding_ids={"F1"},
            evidence_gap_ids=set(),
            asked_gap_ids={"pg7"},
            max_questions=4,
        )
        assert r.dropped[0][1].startswith("gap already asked")

    def test_option_count_bounds(self):
        r = validate_questions(
            [q(options=1)],
            finding_ids={"F1"},
            evidence_gap_ids=set(),
            asked_gap_ids=set(),
            max_questions=4,
        )
        assert "2-5" in r.dropped[0][1]

    def test_hard_cap(self):
        r = validate_questions(
            [q(gap_id=f"pg{i}") for i in range(6)],
            finding_ids={"F1"},
            evidence_gap_ids=set(),
            asked_gap_ids=set(),
            max_questions=4,
        )
        assert len(r.kept) == 4

    def test_fake_option_cites_stripped_not_dropped(self):
        qq = q()
        qq.options[0].cites = ["F42"]  # nonexistent
        r = validate_questions(
            [qq],
            finding_ids={"F1"},
            evidence_gap_ids=set(),
            asked_gap_ids=set(),
            max_questions=4,
        )
        assert r.kept and r.kept[0].options[0].cites == []
