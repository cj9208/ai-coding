"""RecommendationAgent (04): fuse EvidencePack + answers into an auditable
ranked result, then render the report. Steps follow the doc's pipeline:
profile -> candidates -> argument-based scoring -> rank (+adversarial tie) ->
report.
"""

from __future__ import annotations

import logging

from ..contracts.models import (
    AdversarialDraft,
    Assumption,
    EvidencePack,
    Finding,
    PhaseName,
    Pick,
    ProfileDraft,
    Question,
    RecommendationResult,
    Rejected,
    SensitivityNote,
    UserAnswers,
)
from ..contracts.prompts import render_prompt
from ..orchestrator.budget import BudgetView
from ..orchestrator.machine import PhaseOutcome
from ..orchestrator.view import SessionView
from ..research.agent import synthesize_pack
from ..storage.store import SessionStore
from . import scorer
from .profile import default_profile, finalize_profile
from .report import render_markdown

logger = logging.getLogger(__name__)


class RecommendationAgent:
    name = "recommendation"

    def __init__(self, llm, store: SessionStore):
        self.llm = llm
        self.store = store

    async def run(self, view: SessionView, budget: BudgetView) -> PhaseOutcome:
        brief = view.require_brief()
        language = "Chinese" if brief.language.startswith("zh") else "English"
        pack = view.latest_pack or synthesize_pack(view, stop_reason=view.stop_reason)
        findings = view.findings
        finding_ids = {f.id for f in findings}
        answers = merge_answers(view)
        questions = [q for c in view.clarifications for q in c.questions]

        # 1. preference profile ------------------------------------------------
        profile = await self._build_profile(
            view, brief.user_context, answers, questions, findings
        )

        # 2. candidates ---------------------------------------------------------
        candidates = scorer.candidates_from_findings(findings)
        if not candidates:
            result = self._abstain(
                view, pack, profile, "no candidates surfaced in the evidence"
            )
            return self._complete(view, profile, result)

        # 3. argument-based scoring ----------------------------------------------
        draft = await scorer.score_candidates(
            self.llm,
            topic=brief.topic,
            profile=profile,
            candidates=candidates,
            findings=findings,
            language=language,
        )
        ranked = scorer.rank(profile, draft, finding_ids)

        # abstinence rule (04 §3): <2 evidence-backed survivors is a result,
        # not an error
        if len(ranked.order) < 2:
            result = self._abstain(
                view,
                pack,
                profile,
                f"only {len(ranked.order)} candidate(s) survived constraints "
                "with cited evidence",
            )
            result.rejected_notable = _rejecteds(ranked)
            return self._complete(view, profile, result)

        # 4. rank + adversarial tie check -----------------------------------------
        adversarial: AdversarialDraft | None = None
        top = ranked.order[:2]
        margin = abs(ranked.scores[top[0].candidate] - ranked.scores[top[1].candidate])
        if margin <= scorer.TIE_MARGIN:
            try:
                adversarial = await scorer.adversarial_check(
                    self.llm,
                    topic=brief.topic,
                    profile=profile,
                    first=top[0],
                    second=top[1],
                    findings=findings,
                    language=language,
                )
            except Exception as exc:  # noqa: BLE001 — tie check is optional polish
                logger.warning("adversarial pass failed: %s", exc)

        picks = self._make_picks(ranked, pack, profile, findings, adversarial)
        assumptions = self._assumptions(view, pack, questions, answers)
        sensitivity = self._sensitivity(pack, questions, answers, adversarial, ranked)
        result = RecommendationResult(
            session_id=view.session_id,
            picks=picks,
            rejected_notable=_rejecteds(ranked),
            sensitivity=sensitivity,
            assumptions=assumptions,
            overall_confidence=_overall(picks, pack, answers),
            stop_reason=pack.stop_reason,
        )
        return self._complete(view, profile, result)

    # -- steps -------------------------------------------------------------------

    async def _build_profile(
        self,
        view,
        user_context: str,
        answers: UserAnswers | None,
        questions: list[Question],
        findings: list[Finding],
    ):
        universe = {c for f in findings for c in f.decision_criteria}
        has_signal = bool(
            user_context.strip()
            or (answers and any(not a.skipped for a in answers.answers))
        )
        if not has_signal:
            return default_profile(universe, view.session_id)
        skipped = (
            [q for q in questions if _is_skipped(answers, q.id)]
            if answers
            else questions
        )
        brief = view.require_brief()
        prompt = render_prompt(
            "profile",
            LANGUAGE="Chinese" if brief.language.startswith("zh") else "English",
            TOPIC=brief.topic,
            USER_CONTEXT=user_context or "(none)",
            ANSWERS=_answers_text(view, questions),
            SKIPPED="; ".join(f"{q.id}: {q.text} ({q.gap_id})" for q in skipped)
            or "(none)",
            CRITERIA=", ".join(sorted(universe)) or "(none)",
        )
        draft: ProfileDraft = await self.llm.chat_json(
            prompt, schema=ProfileDraft, temperature=0.2
        )
        return finalize_profile(
            draft, criteria_universe=universe, session_id=view.session_id
        )

    def _make_picks(
        self,
        ranked: scorer.Ranked,
        pack: EvidencePack,
        profile,
        findings: list[Finding],
        adversarial: AdversarialDraft | None,
    ) -> list[Pick]:
        by_id = {f.id: f for f in findings}
        picks: list[Pick] = []
        tie = adversarial is not None and adversarial.overturn_strength == "strong"
        for i, a in enumerate(ranked.order[:5], 1):
            cited = [
                e
                for p in a.for_points + a.against_points + a.ratings
                for e in p.evidence
                if e
            ]
            independent = len({by_id[e].capture_id for e in cited if e in by_id})
            top_pick_tie = tie and i <= 2
            picks.append(
                Pick(
                    session_id=pack.session_id,
                    rank=None if top_pick_tie else i,
                    tie_group="top" if top_pick_tie else None,
                    candidate=a.candidate,
                    headline_reason="; ".join(p.text for p in a.for_points[:2]),
                    strengths=a.for_points[:2],
                    watch_outs=a.against_points[:2],
                    constraint_fit="; ".join(
                        r.rationale for r in a.ratings if r.rationale
                    )[:300],
                    evidence=sorted(set(cited)),
                    confidence=_pick_confidence(ranked, a.candidate, independent, pack),
                )
            )
        return picks

    def _assumptions(
        self,
        view,
        pack: EvidencePack,
        questions: list[Question],
        answers: UserAnswers | None,
    ) -> list[Assumption]:
        out: list[Assumption] = []
        if view.clarification:
            for q in questions:
                if _is_skipped(answers, q.id):
                    out.append(
                        Assumption(
                            session_id=view.session_id,
                            text=f"未回答「{q.text}」，按默认假设推荐。",
                            gap_id=q.gap_id,
                            question_id=q.id,
                        )
                    )
        asked = {q.gap_id for q in questions}
        for g in pack.preference_gaps:
            if g.id not in asked:
                out.append(
                    Assumption(
                        session_id=view.session_id,
                        text=f"未追问的偏好假设：{g.description}",
                        gap_id=g.id,
                    )
                )
        return out

    def _sensitivity(
        self, pack, questions, answers, adversarial, ranked
    ) -> list[SensitivityNote]:
        notes: list[SensitivityNote] = []
        if adversarial and adversarial.knife_edge:
            notes.append(
                SensitivityNote(
                    session_id=pack.session_id,
                    trigger=f"top-2 之间：{adversarial.overturn_case[:160]}",
                    effect=adversarial.knife_edge,
                )
            )
        for q in questions:
            if _is_skipped(answers, q.id):
                top2 = " vs ".join(list(ranked.scores)[:2]) or "现有排名"
                notes.append(
                    SensitivityNote(
                        session_id=pack.session_id,
                        trigger=f"若「{q.text}」的实际情况与默认假设相反",
                        effect=f"需要复核 {top2} 的先后顺序",
                    )
                )
        for g in pack.evidence_gaps:
            if g.blocked_criteria:
                notes.append(
                    SensitivityNote(
                        session_id=pack.session_id,
                        trigger=f"证据缺口 {g.id}：{g.description[:120]}",
                        effect=f"补齐后 {','.join(g.blocked_criteria)} 相关评分可能变化",
                    )
                )
        return notes

    def _abstain(
        self, view, pack: EvidencePack, profile, why: str
    ) -> RecommendationResult:
        return RecommendationResult(
            session_id=view.session_id,
            abstained=True,
            missing_for_confidence=(
                why
                + "。下一步建议："
                + (
                    pack.preference_gaps[0].description
                    if pack.preference_gaps
                    else "补充检索："
                    + (
                        pack.evidence_gaps[0].candidate_queries[0]
                        if pack.evidence_gaps
                        and pack.evidence_gaps[0].candidate_queries
                        else "缩小标准范围"
                    )
                )
            ),
            overall_confidence="low",
            stop_reason=pack.stop_reason,
            assumptions=self._assumptions(view, pack, [], None),
        )

    def _complete(
        self, view: SessionView, profile, result: RecommendationResult
    ) -> PhaseOutcome:
        """Report first, then hand artifacts to the orchestrator: if rendering
        crashes, the machine stays in RECOMMEND and a re-run retries — the
        never-crash requirement only applies past the last transition."""
        try:
            path = render_and_save(self.store, view, result, profile)
            logger.info("report written: %s", path)
        except Exception as exc:  # noqa: BLE001 — a result without a file still ships
            logger.exception("report rendering failed: %s", exc)
        return PhaseOutcome(next_phase=PhaseName.DONE, artifacts=[profile, result])


# -- module-level helpers -------------------------------------------------------


def render_and_save(
    store: SessionStore, view: SessionView, result: RecommendationResult, profile
) -> str:
    from ..config import REPORTS_DIR
    from ..research.agent import synthesize_pack as _sp

    pack = view.latest_pack or _sp(view, stop_reason=view.stop_reason)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"{view.session_id}.md"
    path.write_text(render_markdown(view, pack, profile, result), encoding="utf-8")
    store.save_report(view.session_id, str(path))
    return str(path)


def merge_answers(view: SessionView) -> UserAnswers | None:
    if not view.answers:
        return None
    all_ans = [a for ua in view.answers for a in ua.answers]
    comment = " ".join(ua.global_comment for ua in view.answers if ua.global_comment)
    return UserAnswers(answers=all_ans, global_comment=comment)


def _is_skipped(answers: UserAnswers | None, question_id: str) -> bool:
    if not answers:
        return True
    hits = [a for a in answers.answers if a.question_id == question_id]
    if not hits:
        return True
    a = hits[-1]
    return a.skipped or not (a.value or (a.free_text or "").strip())


def _answers_text(view: SessionView, questions: list[Question]) -> str:
    qmap = {q.id: q for q in questions}
    lines = []
    for ua in view.answers:
        for a in ua.answers:
            q = qmap.get(a.question_id)
            if _is_skipped(ua, a.question_id):
                continue
            label = a.value or ""
            if q and a.value:
                opt = next((o.label for o in q.options if o.value == a.value), a.value)
                label = opt
            free = f" — 原话：{a.free_text}" if a.free_text else ""
            lines.append(f"{a.question_id} ({q.text if q else '?'}): {label}{free}")
        if ua.global_comment:
            lines.append(f"补充：{ua.global_comment}")
    return "\n".join(lines) or "(none)"


_STARS = {"high": 2, "medium": 1, "low": 0}


def _pick_confidence(
    ranked: scorer.Ranked, candidate: str, independent_sources: int, pack: EvidencePack
) -> str:
    """04 §5 aggregate: coverage + source independence, capped by stop_reason."""
    cov = ranked.coverage.get(candidate, 0)
    level = (
        "high"
        if cov >= 0.6 and independent_sources >= 2
        else "medium" if cov > 0 else "low"
    )
    if pack.stop_reason and level == "high":
        level = "medium"  # budget-cut research caps confidence (04 §5)
    return level


def _overall(picks: list[Pick], pack: EvidencePack, answers: UserAnswers | None) -> str:
    scores = [_STARS[p.confidence] for p in picks] or [0]
    level = max(0, min(scores[0], scores[1] if len(scores) > 1 else scores[0]))
    if pack.stop_reason or (
        answers
        and any(
            a.skipped or not (a.value or (a.free_text or "").strip())
            for a in answers.answers
        )
    ):
        level = min(level, 1)  # cap at medium
    return {2: "high", 1: "medium", 0: "low"}[level]


def _rejecteds(ranked: scorer.Ranked) -> list[Rejected]:
    out = [
        Rejected(candidate=c, reason="被硬性约束过滤（如预算）") for c in ranked.dropped
    ]
    out += [
        Rejected(candidate=c, reason="没有任何有引用的论证，无法评分")
        for c in ranked.no_evidence
    ]
    if len(ranked.order) > 5:
        last = ranked.order[-1]
        against = "; ".join(p.text for p in last.against_points[:2])
        cited = sorted({e for p in last.against_points for e in p.evidence})
        out.append(
            Rejected(
                candidate=last.candidate,
                reason=against or "综合得分最低",
                evidence=cited,
            )
        )
    return out
