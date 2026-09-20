"""End-to-end offline exercise of the whole machine (05 §5):
FakeAdapter sources + scripted LLM, INTAKE -> AWAIT_USER -> resume -> DONE.

The assertions deliberately check the DESIGN guarantees, not just plumbing:
- every pick's citations exist among persisted findings (traceable or it
  didn't happen);
- captures dedup across batches (budget protection);
- the LLM was called exactly where the phases need it — not per page refresh;
- the HITL pause survives across orchestrator objects (process restart).
"""

from __future__ import annotations

import research_agent.config as config
from research_agent.clarifying import ClarifyingAgent
from research_agent.contracts.models import (
    PhaseName,
    QuestionAnswer,
    RecommendationResult,
    ResearchBrief,
    UserAnswers,
)
from research_agent.orchestrator import Budget, Orchestrator
from research_agent.recommendation import RecommendationAgent
from research_agent.research import ResearchAgent
from research_agent.research.adapters import FakeAdapter

from .conftest import ScriptedLLM

SESSION = "e2e-1"


def _raw_finding(claim, kind, candidates, criteria, quote, conf=0.8):
    return {
        "claim": claim,
        "kind": kind,
        "touches_candidates": candidates,
        "decision_criteria": criteria,
        "quote": quote,
        "confidence": conf,
    }


EXTRACTIONS = [
    # forum.md
    {
        "findings": [
            _raw_finding(
                "Students with a 3000 yuan budget pick Model Y and add RAM later",
                "review",
                ["Model Y", "Model X"],
                ["price"],
                "students on a budget of 3000 yuan consistently",
            )
        ]
    },
    # model-x.md
    {
        "findings": [
            _raw_finding(
                "Model X costs 3299 yuan with 16GB RAM",
                "price",
                ["Model X"],
                ["price"],
                "The Model X costs 3299 yuan with 16GB RAM.",
            ),
            _raw_finding(
                "Model X battery reaches 14 hours",
                "fact",
                ["Model X"],
                ["battery"],
                "Battery life on the Model X reaches 14 hours",
            ),
            _raw_finding(
                "Model X has a low panel failure rate",
                "risk",
                ["Model X"],
                ["reliability"],
                "well-known panel supplier with a low failure rate",
            ),
        ]
    },
    # model-y.md
    {
        "findings": [
            _raw_finding(
                "Model Y costs 2499 yuan with 8GB RAM",
                "price",
                ["Model Y"],
                ["price"],
                "The Model Y is priced at 2499 yuan but ships with only 8GB RAM.",
            ),
            _raw_finding(
                "Model Y battery is around 6 hours",
                "fact",
                ["Model Y"],
                ["battery"],
                "Battery life on the Model Y tops out around 6 hours",
            ),
            _raw_finding(
                "Model Y early batches had SSD issues",
                "risk",
                ["Model Y"],
                ["reliability"],
                "early batches of the Model Y had SSD firmware issues",
            ),
        ]
    },
]

PLAN1 = {
    "sub_queries": [
        {
            "query": "model x vs model y comparison",
            "intent": "comparison",
            "adapter_hint": "web_search",
            "priority": 1,
            "expected_findings": "x",
        },
        {
            "query": "student laptop reviews",
            "intent": "survey",
            "adapter_hint": "web_search",
            "priority": 1,
            "expected_findings": "y",
        },
        {
            "query": "best laptop under 3000 yuan constraint",
            "intent": "constraint",
            "adapter_hint": "web_search",
            "priority": 2,
            "expected_findings": "z",
        },
    ],
    "hypothesized_preference_gaps": [
        {
            "description": "does the budget cap at 3000 yuan",
            "blocked_criteria": ["price"],
        }
    ],
}
PLAN2 = {
    "sub_queries": [
        {"query": "model y reliability risk", "intent": "risk", "priority": 2},
        {"query": "prices 2026", "intent": "recency", "priority": 2},
    ]
}
PLAN3 = {
    "sub_queries": [
        {"query": "model y price 2026", "intent": "constraint", "priority": 1}
    ]
}

REFLECT1 = {
    "coverage": {"comparison": "good", "survey": "adequate", "constraint": "thin"},
    "evidence_gaps": [],
    "preference_gaps": [
        {"description": "budget ceiling unknown", "blocked_criteria": ["price"]}
    ],
    "continue_researching": True,
    "next_queries_hint": ["prices", "risk"],
    "saturated": False,
    "reason": "need priority-2 coverage",
}
REFLECT2 = {
    "coverage": {
        "comparison": "good",
        "survey": "good",
        "risk": "adequate",
        "recency": "thin",
    },
    "evidence_gaps": [
        {
            "description": "live prices not confirmed",
            "blocked_criteria": ["price"],
            "candidate_queries": ["model y price 2026"],
        }
    ],
    "preference_gaps": [
        {"description": "budget ceiling unknown", "blocked_criteria": ["price"]}
    ],
    "continue_researching": False,
    "saturated": True,
    "reason": "no new angles without user input",
}

CLARIFY = {
    "intro": "两个问题会显著影响排名",
    "questions": [
        {
            "gap_id": "pg1",
            "text": "预算是硬上限吗？",
            "why_asking": "F2 显示 Model X 为 3299 元，超出 3000 档位；F1 显示多数学生选 Y。",
            "grounded_in": ["F2", "F1"],
            "options": [
                {"value": "under_3000", "label": "3000 以内", "cites": ["F1"]},
                {"value": "flexible", "label": "可以上浮到 3400"},
            ],
            "answer_shape": "single_choice",
            "skippable": True,
        }
    ],
}

PROFILE = {
    "hard_constraints": [
        {"key": "budget_cny", "value": "<=3000", "from": "ques_1/answer"}
    ],
    "criteria": [
        {"key": "price", "weight": 0.6, "from": "ques_1 -> under_3000"},
        {"key": "battery", "weight": 0.2, "from": "default"},
        {"key": "reliability", "weight": 0.2, "from": "default"},
    ],
    "taste_notes": [],
}

SCORE = {
    "assessments": [
        {
            "candidate": "Model X",
            "for_points": [
                {"text": "14h battery", "evidence": ["F3"]},
                {"text": "low failure rate", "evidence": ["F4"]},
            ],
            "against_points": [{"text": "3299 over budget band", "evidence": ["F2"]}],
            "ratings": [
                {
                    "criterion": "price",
                    "score": 0.4,
                    "rationale": "3299",
                    "evidence": ["F2"],
                },
                {
                    "criterion": "battery",
                    "score": 0.95,
                    "rationale": "14h",
                    "evidence": ["F3"],
                },
                {
                    "criterion": "reliability",
                    "score": 0.9,
                    "rationale": "panels",
                    "evidence": ["F4"],
                },
            ],
        },
        {
            "candidate": "Model Y",
            "for_points": [{"text": "2499 and student pick", "evidence": ["F5", "F1"]}],
            "against_points": [
                {"text": "6h battery", "evidence": ["F6"]},
                {"text": "SSD firmware issues", "evidence": ["F7"]},
            ],
            "ratings": [
                {
                    "criterion": "price",
                    "score": 0.9,
                    "rationale": "2499",
                    "evidence": ["F5"],
                },
                {
                    "criterion": "battery",
                    "score": 0.3,
                    "rationale": "6h",
                    "evidence": ["F6"],
                },
                {
                    "criterion": "reliability",
                    "score": 0.4,
                    "rationale": "SSD",
                    "evidence": ["F7"],
                },
            ],
        },
    ],
    "constraint_dropped": [],
}


def _build(store, fixture_dir) -> tuple[Orchestrator, ScriptedLLM]:
    llm = ScriptedLLM(
        {
            "ResearchPlan": [PLAN1, PLAN2, PLAN3],
            "ExtractionResult": EXTRACTIONS,
            "ReflectDecision": [REFLECT1, REFLECT2],
            "ClarificationDraft": [CLARIFY],
            "ProfileDraft": [PROFILE],
            "ScoringDraft": [SCORE],
        }
    )
    adapters = {"web_search": FakeAdapter(fixture_dir)}
    orch = Orchestrator(
        store,
        research=ResearchAgent(llm, adapters, store),
        clarifying=ClarifyingAgent(llm, store),
        recommender=RecommendationAgent(llm, store),
        budget=Budget(max_collect_calls=30),
    )
    return orch, llm


async def test_full_loop(store, fixture_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    orch, llm = _build(store, fixture_dir)
    store.create_session(SESSION)
    store.append_artifact(
        SESSION,
        ResearchBrief(session_id=SESSION, topic="3000 元档笔记本", language="zh"),
    )

    # ---- phase 1: research, reflect, clarify -> HITL pause --------------------
    phase = await orch.run(SESSION)
    assert phase is PhaseName.AWAIT_USER
    assert store.get_phase(SESSION) == "AWAIT_USER"
    clar_raw = store.latest_artifact(SESSION, "Clarification")
    assert clar_raw and len(clar_raw["questions"]) == 1
    assert llm.calls.count("ExtractionResult") == 3  # per NEW capture only
    assert "ProfileDraft" not in llm.calls  # recommender not started

    # grounding survived the validator with real ids
    assert set(clar_raw["questions"][0]["grounded_in"]) <= {
        f.id for f in store.findings(SESSION)
    }

    # ---- resume in a FRESH orchestrator (the process may have died) ----------
    orch2, llm2 = _build(store, fixture_dir)  # fresh script: deepen + recommend only
    store.transition(
        SESSION,
        PhaseName.DEEPEN,
        artifacts=[
            UserAnswers(
                session_id=SESSION,
                answers=[QuestionAnswer(question_id="ques_1", value="under_3000")],
                global_comment="",
            )
        ],
    )
    phase = await orch2.run(SESSION)
    assert phase is PhaseName.DONE

    # ---- the recommendation: citations all real, ordering evidence-driven ----
    raw = store.latest_artifact(SESSION, "RecommendationResult")
    result = RecommendationResult.model_validate(raw)
    assert not result.abstained
    finding_ids = {f.id for f in store.findings(SESSION)}
    assert result.picks, "expected picks"
    for p in result.picks:
        assert p.evidence and set(p.evidence) <= finding_ids
    assert result.picks[0].candidate == "Model Y"  # 0.6*0.9 beats X's 0.61
    assert result.picks[0].confidence in {"high", "medium"}

    # dedup: 3 batches, still exactly 3 captures (budget protection)
    assert len(store.captures(SESSION)) == 3
    used = store.budget_used(SESSION, "collect_calls")
    assert 0 < used <= 30

    # gap audit trail: pg1 opened -> asked -> resolved by answer
    assert store.open_gaps(SESSION, "preference") == []

    # report file, with citations and sources
    path = store.get_report_path(SESSION)
    text = (tmp_path / "reports" / f"{SESSION}.md").read_text(encoding="utf-8")
    assert path and "Model Y" in text and "`F1`" in text and "## 来源" in text
    assert "3000" in text  # the answer reached the report


async def test_nothing_to_ask_skips_await(store, fixture_dir, tmp_path, monkeypatch):
    """Validator drops an ungrounded question set -> straight to RECOMMEND."""
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    bad = dict(CLARIFY, questions=[dict(CLARIFY["questions"][0], grounded_in=["F999"])])
    llm = ScriptedLLM(
        {
            "ResearchPlan": [PLAN1, PLAN2],
            "ExtractionResult": EXTRACTIONS,
            "ReflectDecision": [REFLECT1, REFLECT2],
            "ClarificationDraft": [bad],
            "ProfileDraft": [PROFILE],
            "ScoringDraft": [SCORE],
        }
    )
    adapters = {"web_search": FakeAdapter(fixture_dir)}
    orch = Orchestrator(
        store,
        research=ResearchAgent(llm, adapters, store),
        clarifying=ClarifyingAgent(llm, store),
        recommender=RecommendationAgent(llm, store),
        budget=Budget(),
    )
    store.create_session("noq")
    store.append_artifact("noq", ResearchBrief(session_id="noq", topic="t"))
    phase = await orch.run("noq")
    # clarify yielded no valid questions -> DEEPEN (no answers -> recommend) -> DONE
    assert phase is PhaseName.DONE
    assert store.latest_artifact("noq", "Clarification")["questions"] == []


async def test_budget_violation_degrades_not_discards(
    store, fixture_dir, tmp_path, monkeypatch
):
    """Zero-ish collect budget: machine must still reach DONE with hedged
    output — failing loudly with no output is worse than a weak answer."""
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    bad = dict(CLARIFY, questions=[dict(CLARIFY["questions"][0], grounded_in=["F999"])])
    llm = ScriptedLLM(
        {
            "ResearchPlan": [PLAN1, PLAN2],
            "ReflectDecision": [REFLECT1, REFLECT2],
            "ClarificationDraft": [bad],
        }
    )
    adapters = {"web_search": FakeAdapter(fixture_dir)}
    orch = Orchestrator(
        store,
        research=ResearchAgent(llm, adapters, store),
        clarifying=ClarifyingAgent(llm, store),
        recommender=RecommendationAgent(llm, store),
        budget=Budget(max_collect_calls=1, max_deepen_calls=0),
    )
    store.create_session("poor")
    store.append_artifact("poor", ResearchBrief(session_id="poor", topic="t"))
    phase = await orch.run("poor")
    assert phase is PhaseName.DONE
    assert store.get_stop_reason("poor") == "budget"
    result = RecommendationResult.model_validate(
        store.latest_artifact("poor", "RecommendationResult")
    )
    # too thin to recommend: the honest abstain — a result, not a crash
    assert result.abstained and result.missing_for_confidence
    assert result.stop_reason == "budget"
