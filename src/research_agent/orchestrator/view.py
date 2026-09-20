"""SessionView: read-only snapshot of one session's artifacts (01 §6).

Subagents are pure functions of their input artifacts — a resumed process
reconstructs everything from the DB, so a view built via ``load`` must carry
every fact a phase needs. Fixture-based tests construct the same view from
JSON files instead (05 §5.1).
"""

from __future__ import annotations

from typing import Type, TypeVar

from pydantic import BaseModel, ValidationError

from ..contracts.models import (
    BatchReport,
    Clarification,
    EvidencePack,
    Finding,
    PreferenceProfile,
    RecommendationResult,
    ReflectDecision,
    ResearchBrief,
    ResearchPlan,
    UserAnswers,
)

A = TypeVar("A", bound=BaseModel)


class ArtifactError(RuntimeError):
    """A persisted artifact failed to validate — corruption, not a model miss."""


class SessionView:
    def __init__(
        self,
        session_id: str,
        phase: str,
        artifacts: dict[str, list[dict]],
        findings: list[Finding] | None = None,
        capture_sources: dict[str, tuple[str, str]] | None = None,
        stop_reason: str | None = None,
    ):
        self.session_id = session_id
        self.phase = phase
        self.stop_reason = stop_reason
        self._artifacts = artifacts  # kind -> [payload, ...] oldest first
        self.findings = findings or []
        self.capture_sources = capture_sources or {}  # capture_id -> (url, title)

    # -- generic accessors -----------------------------------------------------

    def all_of(self, model: Type[A]) -> list[A]:
        kind = model.__name__
        out: list[A] = []
        for payload in self._artifacts.get(kind, []):
            try:
                out.append(model.model_validate(payload))
            except ValidationError as exc:
                raise ArtifactError(
                    f"{kind} artifact failed validation: {exc}"
                ) from exc
        return out

    def latest(self, model: Type[A]) -> A | None:
        items = self.all_of(model)
        return items[-1] if items else None

    def has(self, kind: str) -> bool:
        return bool(self._artifacts.get(kind))

    # -- typed shortcuts ---------------------------------------------------------

    @property
    def brief(self) -> ResearchBrief | None:
        return self.latest(ResearchBrief)

    def require_brief(self) -> ResearchBrief:
        """The orchestrator's INTAKE guard already ruled out a missing brief;
        steps call this instead of propagating Optional through mypy."""
        brief = self.brief
        if brief is None:
            raise ArtifactError("no ResearchBrief on session — INTAKE incomplete")
        return brief

    @property
    def plans(self) -> list[ResearchPlan]:
        return self.all_of(ResearchPlan)

    @property
    def evidence_packs(self) -> list[EvidencePack]:
        return self.all_of(EvidencePack)

    @property
    def batch_reports(self) -> list[BatchReport]:
        return self.all_of(BatchReport)

    @property
    def decisions(self) -> list[ReflectDecision]:
        return self.all_of(ReflectDecision)

    @property
    def latest_pack(self) -> EvidencePack | None:
        return self.evidence_packs[-1] if self.evidence_packs else None

    @property
    def clarification(self) -> Clarification | None:
        return self.latest(Clarification)

    @property
    def clarifications(self) -> list[Clarification]:
        return self.all_of(Clarification)

    @property
    def answers(self) -> list[UserAnswers]:
        return self.all_of(UserAnswers)

    @property
    def profile(self) -> PreferenceProfile | None:
        return self.latest(PreferenceProfile)

    @property
    def result(self) -> RecommendationResult | None:
        return self.latest(RecommendationResult)

    # -- derived helpers used by several phases -----------------------------------

    def finding_ids(self) -> set[str]:
        return {f.id for f in self.findings}

    def asked_gap_ids(self) -> set[str]:
        """Never re-ask a gap that already produced a question (01 §5)."""
        return {q.gap_id for c in self.all_of(Clarification) for q in c.questions}

    def answered_or_skipped_question_ids(self) -> set[str]:
        return {a.question_id for ua in self.answers for a in ua.answers}
