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

import asyncio
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import research_agent.config as config
import research_agent.orchestrator.machine as machine_module
import research_agent.recommendation.agent as recommendation_module
from research_agent.clarifying import ClarifyingAgent
from research_agent.contracts.models import (
    BatchReport,
    EvidencePack,
    Finding,
    PhaseName,
    Pick,
    QuestionAnswer,
    RecommendationResult,
    ResearchBrief,
    UserAnswers,
)
from research_agent.orchestrator import Budget, Orchestrator
from research_agent.orchestrator.machine import PhaseOutcome
from research_agent.persistence import SessionStore
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
    """计数预算耗尽仍运行必要阶段；不等同于 wallclock 的立即离线收尾。"""
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    monkeypatch.setattr(machine_module, "time", SimpleNamespace(monotonic=lambda: 0.0))
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
    assert result.abstained and result.missing_for_confidence
    assert result.stop_reason == "budget"
    assert llm.calls == ["ResearchPlan", "ReflectDecision", "ClarificationDraft"]
    assert "wallclock" not in result.missing_for_confidence
    assert store.budget_used("poor", "collect_calls") == 1
    assert store.get_report_path("poor") == str(tmp_path / "reports" / "poor.md")
    assert (tmp_path / "reports" / "poor.md").is_file()


WALLCLOCK_SESSION = "wallclock-regression"


class _Wallclock:
    def __init__(self, now=2.0):
        self.now = now
        self.reads = 0

    def monotonic(self):
        self.reads += 1
        assert self.reads <= 8, "wallclock loop exceeded eight clock reads"
        return 0.0 if self.reads == 1 else self.now


def _seed_wallclock_session(store, phase, *, with_evidence=True):
    sid = WALLCLOCK_SESSION
    store.create_session(sid)
    store.append_artifact(
        sid,
        ResearchBrief(
            session_id=sid,
            topic="离线超时回归",
            user_context="必须保留我的预算约束，不要补做模型评估",
        ),
    )
    if with_evidence:
        cid, _ = store.add_capture(
            sid,
            url="https://fixtures.invalid/evidence",
            text="已有材料必须保留",
            adapter="fake",
            title="已采集材料",
        )
        finding = Finding(
            session_id=sid,
            id="F1",
            capture_id=cid,
            claim="已有材料必须保留",
            quote="已有材料必须保留",
            touches_candidates=["原有候选"],
            decision_criteria=["cost"],
        )
        store.add_findings(sid, [finding])
        store.append_artifact(
            sid,
            EvidencePack(
                session_id=sid,
                findings=[finding],
                stop_reason="saturated",
            ),
        )
        store.append_artifact(
            sid,
            UserAnswers(
                session_id=sid,
                answers=[QuestionAnswer(question_id="ques_1", value="under_3000")],
            ),
        )
        store.set_budget(sid, "collect_calls", 30)
        store.bump_budget(sid, "collect_calls", 2)
    store.transition(sid, phase.value)


def _wallclock_setup(store, fixture_dir, tmp_path, monkeypatch, *, now=2.0):
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    orch, llm = _build(store, fixture_dir)
    orch.budget = Budget(wallclock_seconds=1)
    clock = _Wallclock(now)
    monkeypatch.setattr(
        machine_module, "time", SimpleNamespace(monotonic=clock.monotonic)
    )
    steps = AsyncMock(wraps=orch._run_step)
    transitions = Mock(wraps=store.transition)
    renders = Mock(wraps=recommendation_module.render_and_save)
    monkeypatch.setattr(orch, "_run_step", steps)
    monkeypatch.setattr(store, "transition", transitions)
    monkeypatch.setattr(recommendation_module, "render_and_save", renders)
    return orch, llm, clock, steps, transitions, renders


@pytest.mark.parametrize(
    "phase",
    [
        PhaseName.INTAKE,
        PhaseName.PLAN,
        PhaseName.COLLECT,
        PhaseName.REFLECT,
        PhaseName.CLARIFY,
        PhaseName.DEEPEN,
        PhaseName.RECOMMEND,
    ],
)
async def test_wallclock_finishes_once_without_running_a_phase(
    store, fixture_dir, tmp_path, monkeypatch, phase
):
    sid = WALLCLOCK_SESSION
    _seed_wallclock_session(
        store,
        phase,
        with_evidence=phase not in {PhaseName.INTAKE, PhaseName.PLAN},
    )
    kinds = ("ResearchBrief", "EvidencePack", "UserAnswers", "PreferenceProfile")
    before = {kind: store.artifacts(sid, kind) for kind in kinds}
    findings_before = [f.model_dump() for f in store.findings(sid)]
    captures_before = [(c.id, c.url, c.text) for c in store.captures(sid)]
    used_before = {name: store.budget_used(sid, name) for name in Budget().caps()}
    orch, llm, clock, steps, transitions, renders = _wallclock_setup(
        store, fixture_dir, tmp_path, monkeypatch
    )

    assert await orch.run(sid) is PhaseName.DONE
    assert clock.reads == 2
    steps.assert_not_awaited()
    assert llm.calls == []
    assert transitions.call_count == (2 if phase is PhaseName.INTAKE else 1)
    call = transitions.call_args
    assert call.args == (sid, "DONE")
    assert call.kwargs["stop_reason"] == "budget"
    assert len(call.kwargs["artifacts"]) == 1
    assert isinstance(call.kwargs["artifacts"][0], RecommendationResult)
    assert call.kwargs.get("bumps") in (None, {})

    history = store.artifacts(sid, "RecommendationResult")
    assert len(history) == 1
    result = RecommendationResult.model_validate(history[0])
    assert result.session_id == sid and result.created_at
    assert result.abstained and result.picks == []
    assert result.overall_confidence == "low"
    assert result.stop_reason == "budget"
    assert "wallclock" in result.missing_for_confidence
    checked_phase = PhaseName.PLAN if phase is PhaseName.INTAKE else phase
    assert checked_phase.value in result.missing_for_confidence
    assert "评估未完成" in result.missing_for_confidence
    assert store.get_phase(sid) == "DONE"
    assert store.get_stop_reason(sid) == "budget"
    assert {kind: store.artifacts(sid, kind) for kind in kinds} == before
    assert [f.model_dump() for f in store.findings(sid)] == findings_before
    assert [(c.id, c.url, c.text) for c in store.captures(sid)] == captures_before
    assert {
        name: store.budget_used(sid, name) for name in Budget().caps()
    } == used_before

    path = store.get_report_path(sid)
    assert path == str(tmp_path / "reports" / f"{sid}.md")
    report_before = Path(path).read_bytes()
    text = report_before.decode("utf-8")
    assert "研究暂不支持有把握的推荐" in text
    assert "wallclock" in text and "stop_reason=budget" in text
    assert "## 排名推荐" not in text
    if findings_before:
        assert "https://fixtures.invalid/evidence" in text
    renders.assert_called_once()

    reopened = SessionStore.open(tmp_path / "agent.db")
    try:
        again, llm_again = _build(reopened, fixture_dir)
        repeat_transitions = Mock(wraps=reopened.transition)
        monkeypatch.setattr(reopened, "transition", repeat_transitions)
        assert await again.run(sid) is PhaseName.DONE
        repeat_transitions.assert_not_called()
        assert llm_again.calls == []
        assert reopened.artifacts(sid, "RecommendationResult") == history
        assert reopened.get_stop_reason(sid) == "budget"
        assert reopened.get_report_path(sid) == path
        assert Path(path).read_bytes() == report_before
        assert {
            name: reopened.budget_used(sid, name) for name in Budget().caps()
        } == used_before
        renders.assert_called_once()
    finally:
        reopened.client.dispose()


@pytest.mark.parametrize("saved_report", [False, True])
async def test_wallclock_preserves_existing_result_and_report(
    store, fixture_dir, tmp_path, monkeypatch, saved_report
):
    sid = WALLCLOCK_SESSION
    _seed_wallclock_session(store, PhaseName.RECOMMEND)
    original = RecommendationResult(
        session_id=sid,
        picks=[
            Pick(
                session_id=sid,
                candidate="原有候选",
                rank=1,
                evidence=["F1"],
                confidence="high",
            )
        ],
        overall_confidence="high",
        stop_reason="coverage-floor",
    )
    store.append_artifact(sid, original)
    history_before = store.artifacts(sid, "RecommendationResult")
    monkeypatch.setattr(config, "REPORTS_DIR", tmp_path / "reports")
    if saved_report:
        seed_orch, _ = _build(store, fixture_dir)
        recommendation_module.render_and_save(
            store, seed_orch.load_view(sid), original, None
        )
    path = tmp_path / "reports" / f"{sid}.md"
    bytes_before = path.read_bytes() if saved_report else None
    orch, llm, clock, steps, transitions, renders = _wallclock_setup(
        store, fixture_dir, tmp_path, monkeypatch
    )

    assert await orch.run(sid) is PhaseName.DONE
    assert clock.reads == 2
    steps.assert_not_awaited()
    assert llm.calls == []
    transitions.assert_called_once()
    assert transitions.call_args.args == (sid, "DONE")
    assert not transitions.call_args.kwargs["artifacts"]
    assert store.artifacts(sid, "RecommendationResult") == history_before
    assert store.get_stop_reason(sid) == "budget"
    assert renders.call_count == (0 if saved_report else 1)
    assert store.get_report_path(sid) == str(path)
    report_bytes = path.read_bytes()
    if saved_report:
        assert report_bytes == bytes_before
    assert "原有候选" in report_bytes.decode("utf-8")
    assert "stop_reason=coverage-floor" in report_bytes.decode("utf-8")

    assert await orch.run(sid) is PhaseName.DONE
    assert store.artifacts(sid, "RecommendationResult") == history_before
    assert path.read_bytes() == report_bytes
    transitions.assert_called_once()
    assert renders.call_count == (0 if saved_report else 1)
    assert llm.calls == []


async def test_wallclock_report_failure_does_not_lose_the_result(
    store, fixture_dir, tmp_path, monkeypatch, caplog
):
    sid = WALLCLOCK_SESSION
    _seed_wallclock_session(store, PhaseName.RECOMMEND)
    orch, llm, clock, steps, transitions, renders = _wallclock_setup(
        store, fixture_dir, tmp_path, monkeypatch
    )
    renders.side_effect = OSError("report directory unavailable")

    assert await orch.run(sid) is PhaseName.DONE
    assert clock.reads == 2
    assert store.get_phase(sid) == "DONE"
    assert store.get_stop_reason(sid) == "budget"
    history = store.artifacts(sid, "RecommendationResult")
    assert len(history) == 1
    assert RecommendationResult.model_validate(history[0]).abstained
    assert store.get_report_path(sid) is None
    assert "wallclock report rendering failed" in caplog.text
    steps.assert_not_awaited()
    assert llm.calls == []
    transitions.assert_called_once()
    renders.assert_called_once()

    assert await orch.run(sid) is PhaseName.DONE
    assert store.artifacts(sid, "RecommendationResult") == history
    transitions.assert_called_once()
    renders.assert_called_once()


async def test_wallclock_result_and_done_roll_back_together(
    store, fixture_dir, tmp_path, monkeypatch
):
    sid = WALLCLOCK_SESSION
    _seed_wallclock_session(store, PhaseName.PLAN, with_evidence=False)
    orch, llm, clock, steps, transitions, renders = _wallclock_setup(
        store, fixture_dir, tmp_path, monkeypatch
    )
    insert = store._insert_artifact

    def fail_after_insert(db, session_id, artifact):
        insert(db, session_id, artifact)
        raise OSError("commit interrupted")

    monkeypatch.setattr(store, "_insert_artifact", fail_after_insert)
    with pytest.raises(OSError, match="commit interrupted"):
        await orch.run(sid)
    assert clock.reads == 2
    assert store.get_phase(sid) == "PLAN"
    assert store.get_stop_reason(sid) is None
    assert store.artifacts(sid, "RecommendationResult") == []
    renders.assert_not_called()
    steps.assert_not_awaited()
    assert llm.calls == []
    transitions.assert_called_once()

    monkeypatch.setattr(store, "_insert_artifact", insert)
    retry_clock = _Wallclock()
    monkeypatch.setattr(
        machine_module, "time", SimpleNamespace(monotonic=retry_clock.monotonic)
    )
    assert await orch.run(sid) is PhaseName.DONE
    assert retry_clock.reads == 2
    assert len(store.artifacts(sid, "RecommendationResult")) == 1
    assert transitions.call_count == 2
    renders.assert_called_once()
    assert llm.calls == []


async def test_wallclock_waits_for_inflight_step_and_preserves_its_outcome(
    store, fixture_dir, tmp_path, monkeypatch
):
    sid = WALLCLOCK_SESSION
    _seed_wallclock_session(store, PhaseName.COLLECT)
    used_before = store.budget_used(sid, "collect_calls")
    orch, llm, clock, steps, transitions, renders = _wallclock_setup(
        store, fixture_dir, tmp_path, monkeypatch, now=0.0
    )
    entered = asyncio.Event()
    release = asyncio.Event()

    async def inflight_collect(view, budget):
        entered.set()
        await release.wait()
        return PhaseOutcome(
            next_phase=PhaseName.REFLECT,
            artifacts=[BatchReport(session_id=view.session_id, calls_used=1)],
            bumps={"collect_calls": 1},
        )

    monkeypatch.setitem(orch._steps, PhaseName.COLLECT, inflight_collect)
    task = asyncio.create_task(orch.run(sid))
    try:
        await asyncio.wait_for(entered.wait(), timeout=1.0)
        clock.now = 2.0
        assert not task.done()
        assert store.get_phase(sid) == "COLLECT"
        assert store.artifacts(sid, "BatchReport") == []
        release.set()
        assert await asyncio.wait_for(task, timeout=1.0) is PhaseName.DONE
    finally:
        release.set()
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    assert clock.reads == 3
    steps.assert_awaited_once()
    assert steps.call_args.args == (sid, PhaseName.COLLECT)
    assert [call.args[1] for call in transitions.call_args_list] == ["REFLECT", "DONE"]
    assert store.budget_used(sid, "collect_calls") == used_before + 1
    assert store.latest_artifact(sid, "BatchReport")["calls_used"] == 1
    result = RecommendationResult.model_validate(
        store.latest_artifact(sid, "RecommendationResult")
    )
    assert result.abstained and result.stop_reason == "budget"
    assert "REFLECT" in result.missing_for_confidence
    assert llm.calls == []
    renders.assert_called_once()
