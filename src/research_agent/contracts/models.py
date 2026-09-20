"""Cross-phase artifacts — the shared vocabulary (05-data-contracts doc).

Single source of truth: other packages import from here and never define
their own cross-phase types. Every artifact carries session_id /
schema_version / created_at so the store can persist full history and replay.

Two families of models live here:
- *Artifacts* (ResearchPlan, EvidencePack, Clarification, ...): what phases
  exchange and what the DB persists.
- *LLM output schemas* (ExtractionResult, ClarificationDraft, ...): exactly
  one per prompt, passed to ``llm_client.chat_json(schema=...)``. Where an
  LLM must not invent identity (finding ids, support keys), the draft model
  omits them and code assigns them — that is the anti-hallucination seam.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = 1

# Distinct vocabularies on purpose (02 §5): evidence gaps are closed by
# research, preference gaps only by asking the user. Downstream treats the
# two lists as disjoint namespaces.
Intent = Literal["survey", "comparison", "constraint", "recency", "risk"]
FindingKind = Literal["fact", "review", "comparison", "price", "risk"]
GapKind = Literal["evidence", "preference"]
CoverageRating = Literal["good", "adequate", "thin", "missing"]
ConfidenceLabel = Literal["high", "medium", "low"]


class PhaseName(str, Enum):
    INTAKE = "INTAKE"
    PLAN = "PLAN"
    COLLECT = "COLLECT"
    REFLECT = "REFLECT"
    CLARIFY = "CLARIFY"
    AWAIT_USER = "AWAIT_USER"
    DEEPEN = "DEEPEN"
    RECOMMEND = "RECOMMEND"
    DONE = "DONE"
    FAILED = "FAILED"


class Artifact(BaseModel):
    """Base for anything persisted on a session."""

    model_config = ConfigDict(
        extra="ignore"
    )  # LLMs like to add fields; ignore, never crash

    session_id: str = ""
    schema_version: int = SCHEMA_VERSION
    created_at: str = ""  # ISO-8601, stamped by the store on write


# ---------------------------------------------------------------------------
# INTAKE / PLAN
# ---------------------------------------------------------------------------


class ResearchBrief(Artifact):
    topic: str
    user_context: str = ""
    candidate_domain_hint: str = ""
    #: language the user-facing text (questions, report) should be written in
    language: str = "zh"


class SubQuery(Artifact):
    id: str = ""
    query: str
    intent: Intent = "survey"
    adapter_hint: str = "web_search"
    priority: int = Field(
        default=1, ge=1, le=3
    )  # 1=must-run 2=if-budget 3=only-if-gaps
    expected_findings: str = ""


class HypothesizedGap(Artifact):
    """Preference unknowns the planner *suspects* up front (02 §2); they are
    seeds, filtered later by what research actually left open."""

    description: str
    blocked_criteria: list[str] = Field(default_factory=list)


class ResearchPlan(Artifact):
    sub_queries: list[SubQuery] = Field(default_factory=list)
    hypothesized_preference_gaps: list[HypothesizedGap] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# COLLECT: captures -> findings
# ---------------------------------------------------------------------------


class RawFinding(BaseModel):
    """What the extractor LLM emits — no ids, no provenance; code fills those."""

    model_config = ConfigDict(extra="ignore")

    claim: str  # one atomic, checkable statement
    kind: FindingKind = "fact"
    touches_candidates: list[str] = Field(default_factory=list)
    decision_criteria: list[str] = Field(default_factory=list)
    quote: str  # must be a verbatim span of the capture; the normalizer audits it
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class ExtractionResult(BaseModel):
    """Schema for the extraction prompt."""

    model_config = ConfigDict(extra="ignore")

    findings: list[RawFinding] = Field(default_factory=list)


class Finding(Artifact):
    id: str  # "F1" — user-visible citation, stable once written
    capture_id: str  # "C1" — provenance
    claim: str
    kind: FindingKind = "fact"
    touches_candidates: list[str] = Field(default_factory=list)
    decision_criteria: list[str] = Field(default_factory=list)
    quote: str
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    support_key: str = ""  # near-dup cluster id; independence counts ride on it
    batch: int = 0  # collection round; feeds the saturation heuristic (02 §4)


class SourceFailure(Artifact):
    query_id: str = ""
    adapter: str = ""
    url: str = ""
    error: str = ""


class BatchReport(Artifact):
    """What one COLLECT round cost and produced — the audit ledger of the
    collector, aggregated later into EvidencePack.source_failures."""

    batch: int = 0
    calls_used: int = 0
    quotes_rejected: int = 0
    captures_new: int = 0
    captures_deduped: int = 0
    new_finding_ids: list[str] = Field(default_factory=list)
    failures: list[SourceFailure] = Field(default_factory=list)


class EvidenceGap(Artifact):
    id: str
    kind: GapKind
    description: str
    blocked_criteria: list[str] = Field(default_factory=list)
    candidate_queries: list[str] = Field(
        default_factory=list
    )  # empty for preference gaps


class GapDraft(BaseModel):
    """Reflector emits gaps inside named lists; code assigns `kind` from the list."""

    model_config = ConfigDict(extra="ignore")

    id: str = ""
    description: str
    blocked_criteria: list[str] = Field(default_factory=list)
    candidate_queries: list[str] = Field(default_factory=list)


class ReflectDecision(Artifact):
    """Reflector's loop-control output (02 §4) — judged over a digest, not raw text."""

    coverage: dict[str, CoverageRating] = Field(default_factory=dict)
    evidence_gaps: list[GapDraft] = Field(default_factory=list)
    preference_gaps: list[GapDraft] = Field(default_factory=list)
    continue_researching: bool = True
    next_queries_hint: list[str] = Field(default_factory=list)
    saturated: bool = False
    reason: str = ""


class EvidencePack(Artifact):
    """The Research Agent's single deliverable, consumed by 03 and 04."""

    findings: list[Finding] = Field(default_factory=list)
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    preference_gaps: list[EvidenceGap] = Field(default_factory=list)
    source_failures: list[SourceFailure] = Field(default_factory=list)
    coverage: dict[str, CoverageRating] = Field(default_factory=dict)
    stop_reason: Optional[str] = (
        None  # "budget" | "saturated" | "coverage-floor" | None
    )


# ---------------------------------------------------------------------------
# CLARIFY / AWAIT_USER
# ---------------------------------------------------------------------------


class Option(BaseModel):
    model_config = ConfigDict(extra="ignore")

    value: str  # English-stable key for scoring
    label: str  # user's language
    cites: list[str] = Field(default_factory=list)  # finding ids; empty => "user's own"


class Question(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    gap_id: str
    text: str
    why_asking: str = ""  # mandatory by validator: the one surface showing homework
    grounded_in: list[str] = Field(
        default_factory=list
    )  # validator: >=1 real finding id
    options: list[Option] = Field(default_factory=list)
    answer_shape: Literal["single_choice", "multi_choice"] = "single_choice"
    skippable: bool = True


class Clarification(Artifact):
    """Empty ``questions`` means nothing_to_ask — a first-class, common output."""

    questions: list[Question] = Field(default_factory=list)
    intro: str = ""
    round: int = 1


class ClarificationDraft(BaseModel):
    """Schema for the clarifying prompt (ids assigned by code)."""

    model_config = ConfigDict(extra="ignore")

    intro: str = ""
    questions: list[Question] = Field(default_factory=list)


class QuestionAnswer(BaseModel):
    model_config = ConfigDict(extra="ignore")

    question_id: str
    value: Optional[str] = None  # chosen option key
    free_text: Optional[str] = None
    skipped: bool = False


class UserAnswers(Artifact):
    answers: list[QuestionAnswer] = Field(default_factory=list)
    global_comment: str = ""
    round: int = 1


# ---------------------------------------------------------------------------
# RECOMMEND
# ---------------------------------------------------------------------------


class HardConstraint(BaseModel):
    """Filter, don't score (04 §2)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str
    value: str
    source: str = Field(default="", alias="from")


class Criterion(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    key: str
    weight: float = Field(default=0.0, ge=0.0, le=1.0)
    source: str = Field(default="", alias="from")  # answer / quote / "default (...)"
    direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"


class PreferenceProfile(Artifact):
    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    taste_notes: list[str] = Field(default_factory=list)


class ProfileDraft(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    hard_constraints: list[HardConstraint] = Field(default_factory=list)
    criteria: list[Criterion] = Field(default_factory=list)
    taste_notes: list[str] = Field(default_factory=list)


class CriterionRating(BaseModel):
    model_config = ConfigDict(extra="ignore")

    criterion: str
    score: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # None => abstain
    rationale: str = ""
    evidence: list[str] = Field(default_factory=list)


class EvidencedPoint(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str
    evidence: list[str] = Field(default_factory=list)


class Assessment(BaseModel):
    """Argument-before-number (04 §3): for/against first, rating derived from them."""

    model_config = ConfigDict(extra="ignore")

    candidate: str
    for_points: list[EvidencedPoint] = Field(default_factory=list)
    against_points: list[EvidencedPoint] = Field(default_factory=list)
    ratings: list[CriterionRating] = Field(default_factory=list)


class ScoringDraft(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assessments: list[Assessment] = Field(default_factory=list)
    constraint_dropped: list[str] = Field(
        default_factory=list
    )  # dropped by hard constraints


class AdversarialDraft(BaseModel):
    """'Make the best case #2 beats #1' (04 §3). If as strong => admitted tie."""

    model_config = ConfigDict(extra="ignore")

    overturn_case: str = ""
    overturn_strength: Literal["weak", "moderate", "strong"] = "weak"
    knife_edge: str = ""  # "pick A if X, B if Y"


class Pick(Artifact):
    rank: Optional[int] = None  # None => part of a declared tie group
    tie_group: Optional[str] = None
    candidate: str
    headline_reason: str = ""
    strengths: list[EvidencedPoint] = Field(default_factory=list)
    watch_outs: list[EvidencedPoint] = Field(default_factory=list)
    constraint_fit: str = ""
    evidence: list[str] = Field(default_factory=list)
    confidence: ConfidenceLabel = "medium"


class Rejected(Artifact):
    candidate: str
    reason: str
    evidence: list[str] = Field(default_factory=list)


class SensitivityNote(Artifact):
    trigger: str  # "if <gap/answer> flipped"
    effect: str


class Assumption(Artifact):
    text: str
    gap_id: str = ""
    question_id: str = ""


class RecommendationResult(Artifact):
    picks: list[Pick] = Field(default_factory=list)
    rejected_notable: list[Rejected] = Field(default_factory=list)
    sensitivity: list[SensitivityNote] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    overall_confidence: ConfidenceLabel = "medium"
    stop_reason: Optional[str] = None  # propagated from research
    #: honest output when <2 candidates survive with evidence (04 §3) — a result, not an error
    abstained: bool = False
    missing_for_confidence: str = ""
