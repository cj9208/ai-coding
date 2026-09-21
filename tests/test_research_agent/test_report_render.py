"""report.render_markdown — pure rendering over hand-built artifacts.

The report is the one user-facing artifact that must be checkable without an
LLM or a DB: pick/tie/abstain/source-failure shapes are asserted verbatim.
"""

from research_agent.contracts.models import (
    Assumption,
    Criterion,
    EvidencePack,
    Finding,
    Pick,
    PreferenceProfile,
    RecommendationResult,
    Rejected,
    ResearchBrief,
    SensitivityNote,
    SourceFailure,
)
from research_agent.orchestrator.view import SessionView
from research_agent.recommendation.report import render_markdown


def _finding(fid: str, capture_id: str = "C1") -> Finding:
    return Finding(
        session_id="s1", id=fid, capture_id=capture_id, claim=f"claim {fid}", quote="q"
    )


def _view() -> SessionView:
    brief = ResearchBrief(session_id="s1", topic="轻薄本")
    return SessionView(
        session_id="s1",
        phase="DONE",
        artifacts={"ResearchBrief": [brief.model_dump()]},
        findings=[_finding("F1")],
        capture_sources={"C1": ("https://dev.example/a", "评测 A")},
    )


def _result(**over) -> RecommendationResult:
    payload = dict(
        session_id="s1",
        picks=[
            Pick(
                session_id="s1",
                rank=1,
                candidate="X1",
                headline_reason="够轻",
                evidence=["F1"],
                confidence="high",
            )
        ],
        overall_confidence="high",
    )
    payload.update(over)
    return RecommendationResult(**payload)


def _pack(**over) -> EvidencePack:
    payload = dict(session_id="s1", findings=[_finding("F1")])
    payload.update(over)
    return EvidencePack(**payload)


def test_top_pick_stars_and_footer_render():
    md = render_markdown(_view(), _pack(), None, _result())

    assert "# Recommendation: 轻薄本" in md
    assert "首选：X1" in md and "★★★★☆" in md
    assert "整体置信度 high" in md
    assert "- 证据：`F1`" in md  # evidence stays cited by finding id
    assert "session `s1`" in md  # footer lets a human re-find the run


def test_budget_stop_and_coverage_and_profile_render():
    pack = _pack(coverage={"battery": "thin"}, stop_reason="budget")
    profile = PreferenceProfile(
        session_id="s1", criteria=[Criterion(key="price", weight=0.5, source="answer")]
    )

    md = render_markdown(_view(), pack, profile, _result(stop_reason="budget"))

    assert "研究因预算提前停止" in md
    assert "研究覆盖面：battery:thin" in md
    assert "price(50%)" in md


def test_sources_section_walks_f_ids_back_to_urls():
    md = render_markdown(_view(), _pack(), None, _result())

    assert "[评测 A](https://dev.example/a)" in md
    assert "（F1）" in md  # C1's findings are named


def test_source_failures_are_listed_not_hidden():
    pack = _pack(
        source_failures=[
            SourceFailure(
                session_id="s1",
                query_id="q1",
                adapter="web_search",
                url="https://dead.example",
                error="timeout",
            )
        ]
    )

    md = render_markdown(_view(), pack, None, _result())

    assert "## 采集失败记录" in md
    assert "q1 / web_search" in md and "timeout" in md


def test_rejected_and_sensitivity_and_assumptions_render():
    result = _result(
        rejected_notable=[
            Rejected(session_id="s1", candidate="Y", reason="超预算", evidence=["F1"])
        ],
        sensitivity=[
            SensitivityNote(session_id="s1", trigger="预算放宽", effect="Y 反超")
        ],
        assumptions=[Assumption(session_id="s1", text="默认每天通勤")],
    )

    md = render_markdown(_view(), _pack(), None, result)

    assert "**Y** — 超预算" in md
    assert "预算放宽 → Y 反超" in md
    assert "默认每天通勤" in md


def test_tie_renders_equal_first_and_a_warning():
    result = _result(
        picks=[
            Pick(session_id="s1", rank=None, tie_group="top", candidate="X1"),
            Pick(session_id="s1", rank=None, tie_group="top", candidate="X2"),
        ]
    )

    md = render_markdown(_view(), _pack(), None, result)

    assert "首选：X1 / X2" in md
    assert "## 接近并列" in md


def test_abstention_is_a_result_not_an_error():
    result = _result(picks=[], abstained=True, missing_for_confidence="证据不足")

    md = render_markdown(_view(), _pack(), None, result)

    assert "研究暂不支持有把握的推荐" in md
    assert "证据不足" in md
    assert "## 排名推荐" not in md
