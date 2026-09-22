"""The seven runtime objects (CH02_01) plus the signal/value objects the
decision tables read. Field-for-field this is the ``docs/orchestrator-design.md``
"Runtime Objects" section; the durable seven are what the store persists, and
the ``*Signals`` inputs are what ``policy.py`` tables evaluate.

Design posture (why these exist, per CH02_01): separate what the system
*knows* (interpretation), what it *decides* (routing), what it *does*
(execution), and what it *accepts* (outcome) — never collapse them.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Callable, ClassVar, Union

from pydantic import BaseModel, ConfigDict, Field

from .config import Budget
from .ids import new_id


class _Contract(BaseModel):
    """Shared config: tolerate unknown fields on load (forward-compatible
    re-reads), reject on write is not needed for a persisted-only store."""

    model_config = ConfigDict(extra="ignore")


# --------------------------------------------------------------------------
# enums (string-valued; JSON-native)
# --------------------------------------------------------------------------
class RequestStatus(StrEnum):
    captured = "captured"
    interpreting = "interpreting"
    awaiting_clarification = "awaiting_clarification"
    routing = "routing"
    executing = "executing"
    validating = "validating"
    completed = "completed"
    handoff = "handoff"
    rejected = "rejected"
    failed = "failed"


class Decision(StrEnum):
    """Canonical routing outcomes (CH02_01); ``reinterpret``/``retry_execution``
    both record as ``retry`` per CH02_02."""

    proceed = "proceed"
    proceed_conservative = "proceed_conservative"
    clarify = "clarify"
    stronger_model = "stronger_model"
    execute_capability = "execute_capability"
    retry = "retry"
    switch_capability = "switch_capability"
    handoff_human = "handoff_human"
    reject = "reject"
    accept = "accept"
    partial_answer = "partial_answer"
    failed = "failed"


class OutcomeType(StrEnum):
    answered = "answered"
    clarification_requested = "clarification_requested"
    partial_answer = "partial_answer"
    human_handoff = "human_handoff"
    rejected = "rejected"
    failed = "failed"


class ConfidenceState(StrEnum):
    clear = "clear"
    weak_but_usable = "weak_but_usable"
    ambiguous = "ambiguous"
    unsafe = "unsafe"
    blocked = "blocked"


class SafetyDecision(StrEnum):
    allow = "allow"
    constrain = "constrain"
    clarify_scope = "clarify_scope"
    refuse = "refuse"
    handoff = "handoff"


class RiskLevel(StrEnum):
    low = "low"
    medium = "medium"
    high = "high"


class ActionType(StrEnum):
    read_only = "read_only"
    write = "write"


class CapReached(StrEnum):
    """Which escalation branch hit its ceiling (CH02_02 fallback table)."""

    reinterpretation_cap = "reinterpretation_cap"
    clarification_cap = "clarification_cap"
    execution_retry_cap = "execution_retry_cap"
    model_escalation_cap = "model_escalation_cap"
    total_loop_cap = "total_loop_cap"


class ResultStatus(StrEnum):
    """A capability execution's self-reported result class."""

    success = "success"
    weak = "weak"
    failed = "failed"


# --------------------------------------------------------------------------
# object 1: RequestEnvelope (DP-8: budgets + counters + state all live here)
# --------------------------------------------------------------------------
class ExecutionBudget(_Contract):
    max_total_loops: int = Budget.MAX_TOTAL_LOOPS
    max_tool_calls: int = Budget.MAX_TOOL_CALLS
    max_reinterpretations: int = Budget.MAX_REINTERPRETATIONS
    max_execution_retries: int = Budget.MAX_EXECUTION_RETRIES
    max_clarification_turns: int = Budget.MAX_CLARIFICATION_TURNS
    max_model_escalations: int = Budget.MAX_MODEL_ESCALATIONS
    max_wall_clock_ms: int = Budget.MAX_WALL_CLOCK_MS
    #: 05c quota gate: per-user *daily* allowance of LLM calls, day-grain
    #: (UTC calendar day in v1). Not a loop cap — it is compared against
    #: today's consumed calls across the user's requests, and it has its
    #: own routing row (route_r10), deliberately not a CapReached line.
    max_llm_calls_per_day: int = Budget.LLM_CALLS_PER_DAY
    #: time the request spent parked in ``awaiting_clarification`` across
    #: process boundaries; DP-4's wall clock budgets machine work, not the
    #: human's answer latency, so it is subtracted at every wait->resume edge
    wall_clock_paused_ms: int = 0


class AttemptCounters(_Contract):
    """Live usage beside the limits (design Open Question #3 resolved: two
    sibling dicts, not interleaved)."""

    total_loops: int = 0
    reinterpretations: int = 0
    execution_retries: int = 0
    clarification_turns: int = 0
    model_escalations: int = 0
    tool_calls: int = 0
    #: LLM calls consumed by *this* request (front-half model passes +
    #: capability-reported calls). Summed across the user's requests for
    #: the day by ``Store.llm_calls_today`` (05c quota gate).
    llm_calls: int = 0


class OriginalInput(_Contract):
    model_config = ConfigDict(extra="ignore", frozen=True)  # write-once (DP)
    text: str
    attachments: list[str] = Field(default_factory=list)
    locale: str = "zh"


class ContextRefs(_Contract):
    chat_history_ref: str | None = None
    prior_request_refs: list[str] = Field(default_factory=list)
    tenant_id: str = "default"


class PolicyContext(_Contract):
    identity_tier: str = "anonymous"
    permission_profile: str = "public"
    risk_profile: RiskLevel = RiskLevel.low
    data_scope: str = "internal"


class StateRefs(_Contract):
    current_status: RequestStatus = RequestStatus.captured
    selected_domain: str | None = None
    selected_capability: str | None = None
    final_outcome_ref: str | None = None
    #: wall-clock instant of entering awaiting_clarification; consumed into
    #: ``ExecutionBudget.wall_clock_paused_ms`` at the resume edge
    wait_started_ms: int | None = None
    #: optimistic-concurrency token, mirrored from ``requests.version`` — the
    #: column is the authority on read, the bump happens on write
    #: (docs/orchestrator/05a-data-plane.md §B)
    version: int = 1
    #: next ``runtime_objects.seq`` for this request; per-request state, so
    #: it belongs on the envelope (DP-8) rather than a MAX(seq)+1 read
    next_seq: int = 1


class RequestEnvelope(_Contract):
    request_id: str
    session_id: str
    user_id: str
    entrypoint: str = "cli"
    timestamp_start_ms: int
    original_input: OriginalInput
    context: ContextRefs = Field(default_factory=ContextRefs)
    policy_context: PolicyContext = Field(default_factory=PolicyContext)
    execution_budget: ExecutionBudget = Field(default_factory=ExecutionBudget)
    attempt_counters: AttemptCounters = Field(default_factory=AttemptCounters)
    state: StateRefs = Field(default_factory=StateRefs)
    #: identity of the config artifact that processed this request
    #: (``registry.config_hash``); ``None`` for rows written before 05d
    #: step 3 or by code fixtures with no artifact
    config_hash: str | None = None

    @staticmethod
    def new(
        *,
        text: str,
        user_id: str = "cli_user",
        session_id: str | None = None,
        locale: str = "zh",
        policy_context: PolicyContext | None = None,
        budget: ExecutionBudget | None = None,
        config_hash: str | None = None,
    ) -> "RequestEnvelope":
        import time

        return RequestEnvelope(
            request_id=new_id("req"),
            session_id=session_id or new_id("sess"),
            user_id=user_id,
            timestamp_start_ms=int(time.time() * 1000),
            original_input=OriginalInput(text=text, locale=locale),
            policy_context=policy_context or PolicyContext(),
            # own copy: the runtime mutates the budget (wall-clock pause),
            # so a caller-reused template must never be aliased by two live
            # envelopes (DP-8: per-request state is per-request)
            execution_budget=budget.model_copy() if budget else ExecutionBudget(),
            config_hash=config_hash,
        )


# --------------------------------------------------------------------------
# object 2: InterpretationRecord
# --------------------------------------------------------------------------
class DeterministicSignals(_Contract):
    alias_hits: list[str] = Field(default_factory=list)
    rule_hits: list[str] = Field(default_factory=list)
    top_match_score: float | None = None
    top2_gap: float | None = None
    candidate_count: int = 0


class ModelSignals(_Contract):
    model_name: str = "flash"
    confidence: float = 0.0
    ambiguity_flags: list[str] = Field(default_factory=list)
    alternative_interpretations: list[str] = Field(default_factory=list)


class CandidateDomain(_Contract):
    domain: str
    score: float


class InterpretationRecord(_Contract):
    interpretation_id: str
    request_id: str
    timestamp_ms: int
    normalized_query: str
    task_type: str
    deterministic: DeterministicSignals = Field(default_factory=DeterministicSignals)
    model: ModelSignals = Field(default_factory=ModelSignals)
    candidate_domains: list[CandidateDomain] = Field(default_factory=list)
    target_entity_guess: str | None = None
    requested_attributes: list[str] = Field(default_factory=list)
    interpretation_summary: str = ""


# --------------------------------------------------------------------------
# value object: ConfidenceAssessment (embedded in routing reason, DP-9)
# --------------------------------------------------------------------------
class ConfidenceAssessment(_Contract):
    stage: str = "routing"
    confidence_state: ConfidenceState = ConfidenceState.clear
    aggregate_score: float = 0.0  # recorded for calibration, never a condition
    rationale_primary: str = ""
    supporting_signals: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# object 3: RoutingDecision
# --------------------------------------------------------------------------
class DecisionReason(_Contract):
    primary: str
    supporting_signals: list[str] = Field(default_factory=list)
    table_row_id: str = ""  # DP-7: which row fired


class NextAction(_Contract):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RoutingDecision(_Contract):
    routing_decision_id: str
    request_id: str
    interpretation_id: str | None
    timestamp_ms: int
    decision: Decision
    decision_reason: DecisionReason
    selected_domain: str | None = None
    selected_capability: str | None = None
    fallback_capability: str | None = None
    constraints: dict[str, Any] = Field(default_factory=dict)
    next_action: NextAction


# --------------------------------------------------------------------------
# object 4: CapabilityExecutionRecord
# --------------------------------------------------------------------------
class PolicyCheck(_Contract):
    allowed: bool
    permission_profile: str = "public"
    risk_decision: str = "approved"


class ExecutionResult(_Contract):
    status: ResultStatus
    output_ref: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence_signals: dict[str, float] = Field(default_factory=dict)


class CapabilityExecutionRecord(_Contract):
    execution_id: str
    request_id: str
    routing_decision_id: str
    timestamp_start_ms: int
    timestamp_end_ms: int
    domain: str
    capability_name: str
    capability_version: str
    tool_bundle_loaded: list[str] = Field(default_factory=list)
    policy_check: PolicyCheck
    result: ExecutionResult
    execution_plan: list[str] = Field(default_factory=list)  # observational, DP-6
    duration_ms: int = 0


# --------------------------------------------------------------------------
# object 5: FinalOutcome
# --------------------------------------------------------------------------
class ValidationSummary(_Contract):
    relevance: str = "pass"
    completeness: str = "pass"
    grounding: str = "pass"
    policy_compliance: str = "pass"


class FinalOutcome(_Contract):
    final_outcome_id: str
    request_id: str
    timestamp_ms: int
    outcome_type: OutcomeType
    user_response: str = ""
    validation_summary: ValidationSummary = Field(default_factory=ValidationSummary)
    used_execution_ids: list[str] = Field(default_factory=list)
    fallback_history: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# object 6: HandoffPacket (six CH01 sections, DP-3)
# --------------------------------------------------------------------------
class HandoffReason(_Contract):
    code: str
    summary: str


class ConversationContext(_Contract):
    original_input: str
    clarification_history: list[str] = Field(default_factory=list)


class CurrentInterpretation(_Contract):
    normalized_query: str
    candidate_entities: list[str] = Field(default_factory=list)


class AttemptHistory(_Contract):
    routing_decision_ids: list[str] = Field(default_factory=list)
    execution_ids: list[str] = Field(default_factory=list)


class BudgetState(_Contract):
    total_loops_used: int = 0
    clarification_turns_used: int = 0
    model_escalations_used: int = 0
    execution_retries_used: int = 0


class RecommendedNextStep(_Contract):
    type: str
    payload: str = ""


class HandoffPacket(_Contract):
    handoff_id: str
    request_id: str
    timestamp_ms: int
    reason: HandoffReason
    conversation_context: ConversationContext
    current_interpretation: CurrentInterpretation
    attempt_history: AttemptHistory
    budget_state: BudgetState
    recommended_next_step: RecommendedNextStep


# --------------------------------------------------------------------------
# object 7: CapabilityCatalogEntry (config-time)
# --------------------------------------------------------------------------
class OutputContract(_Contract):
    required_fields: list[str] = Field(default_factory=list)
    optional_fields: list[str] = Field(default_factory=list)


class CapabilityCatalogEntry(_Contract):
    name: str
    owner: str
    domain_scope: str
    capability_version: str
    rollout_status: str
    use_when: list[str] = Field(default_factory=list)
    avoid_when: list[str] = Field(default_factory=list)
    tool_schema_bundle: list[str] = Field(default_factory=list)
    output_contract: OutputContract = Field(default_factory=OutputContract)
    fallbacks: list[str] = Field(default_factory=list)
    # optional
    display_name: str | None = None
    purpose: str = ""
    task_types_supported: list[str] = Field(default_factory=list)
    required_inputs: list[str] = Field(default_factory=list)
    optional_inputs: list[str] = Field(default_factory=list)
    preconditions: list[str] = Field(default_factory=list)
    loading_mode: str = "single_capability"
    confidence_signals: list[str] = Field(default_factory=list)
    validation_rules: list[str] = Field(default_factory=list)
    cost_profile: RiskLevel = RiskLevel.low
    latency_profile: RiskLevel = RiskLevel.low
    risk_profile: RiskLevel = RiskLevel.low
    human_escalation_required_when: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    REQUIRED_FIELDS: ClassVar[tuple[str, ...]] = (
        "name",
        "owner",
        "domain_scope",
        "capability_version",
        "rollout_status",
        "use_when",
        "avoid_when",
        "tool_schema_bundle",
        "output_contract",
        "fallbacks",
    )


# --------------------------------------------------------------------------
# capability protocol I/O (also used by fakes)
# --------------------------------------------------------------------------
class CapabilityContext(_Contract):
    request_id: str
    session_id: str
    normalized_query: str
    task_type: str
    requested_attributes: list[str] = Field(default_factory=list)
    policy_context: PolicyContext = Field(default_factory=PolicyContext)
    constraints: dict[str, Any] = Field(default_factory=dict)


class CapabilityResult(_Contract):
    """A capability's structured return. It reports status and never decides
    its own retry/escalation (routing does) — CH02_02 clean-failure rule."""

    status: ResultStatus
    code: str | None = None
    output: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    confidence_signals: dict[str, float] = Field(default_factory=dict)
    tool_steps: list[str] = Field(default_factory=list)
    #: how many LLM calls this run consumed — the capability reports it
    #: (it is the only party that knows), the runtime adds it to the
    #: envelope's ``llm_calls``; it never decides anything itself (05c)
    llm_calls: int = 0


# --------------------------------------------------------------------------
# front-half seam output (M0: produced by a fake; M1: by interpret.py)
# --------------------------------------------------------------------------
class FrontHalfOutput(_Contract):
    """Everything routing needs from one interpretation pass. The front half
    (safety gate + normalize + model interpret) is the only probabilistic
    side of the seam; this object is how it reports deterministically."""

    interpretation: InterpretationRecord
    safety: SafetyDecision = SafetyDecision.allow
    action_type: ActionType = ActionType.read_only
    risk: RiskLevel = RiskLevel.low
    missing_required_constraint: bool = False
    user_resolvable_ambiguity: bool = False
    clarification_question: str = ""
    constraints: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------
# one turn's user-facing result (CLI projection, not a persisted object)
# --------------------------------------------------------------------------
class TurnResult(_Contract):
    request_id: str
    status: RequestStatus
    outcome_type: OutcomeType | None = None
    response: str = ""
    question: str = ""
    handoff_id: str | None = None
    routing_decision_ids: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# golden cases (CH04 regression units; consumed by golden.py)
# --------------------------------------------------------------------------
class GoldenExpectation(_Contract):
    status: RequestStatus
    outcome: OutcomeType | None = None
    #: decision-table rows that must have fired, in order (subsequence)
    fired_row_ids: list[str] = Field(default_factory=list)


class GoldenCase(_Contract):
    case_id: str
    input: str
    #: FakeFrontHalf script — one dict per interpret call
    turns: list[dict[str, Any]] = Field(default_factory=list)
    capability: str = "echo"
    #: scripted CapabilityResult dicts (empty -> EchoCapability)
    results: list[dict[str, Any]] = Field(default_factory=list)
    required_fields: list[str] = Field(default_factory=lambda: ["text"])
    validation_rules: list[str] = Field(default_factory=list)
    fallback_name: str | None = None
    fallback_required_fields: list[str] = Field(default_factory=lambda: ["text"])
    #: clarification answers fed via resume(), in order
    resume: list[str] = Field(default_factory=list)
    #: ExecutionBudget field overrides
    budget: dict[str, int] = Field(default_factory=dict)
    expected: GoldenExpectation


class GoldenResult(_Contract):
    case_id: str
    ok: bool
    diffs: list[str] = Field(default_factory=list)
    fired_row_ids: list[str] = Field(default_factory=list)
    status: RequestStatus
    outcome: OutcomeType | None = None


# --------------------------------------------------------------------------
# signal inputs for the decision tables (policy.py)
# --------------------------------------------------------------------------
class RoutingSignals(_Contract):
    """Everything the CH01 routing table reads, assembled by the runtime from
    the safety gate + ConfidenceAssessment + risk context + budget state."""

    safety: SafetyDecision = SafetyDecision.allow
    policy_violation: bool = False
    risk: RiskLevel = RiskLevel.low
    action_type: ActionType = ActionType.read_only
    confidence_state: ConfidenceState = ConfidenceState.clear
    missing_required_constraint: bool = False
    user_resolvable_ambiguity: bool = False
    strong_evidence: bool = False
    any_budget_exhausted: bool = False
    #: 05c quota gate: today's consumed LLM calls for this user are at or
    #: over ``max_llm_calls_per_day`` (boolean per DP-9 — a level, never a
    #: weighted score; the row reads this, never the raw counts)
    quota_exhausted: bool = False
    model_escalation_budget_left: bool = True
    aggregate_score: float = 0.0  # carried for the record, never a condition

    # derived predicates used by the table rows (DP-9: state, not score)
    @property
    def policy_block(self) -> bool:
        return self.policy_violation or self.safety == SafetyDecision.refuse

    @property
    def confidence_strong(self) -> bool:
        return self.confidence_state == ConfidenceState.clear

    @property
    def high_risk_or_write(self) -> bool:
        return self.risk == RiskLevel.high or self.action_type == ActionType.write

    @property
    def read_only_low_risk(self) -> bool:
        return self.action_type == ActionType.read_only and self.risk == RiskLevel.low

    @property
    def confidence_weak(self) -> bool:
        return self.confidence_state in (
            ConfidenceState.ambiguous,
            ConfidenceState.weak_but_usable,
        )


class ExecutionSignals(_Contract):
    """Input to the CH02_03 execution table (what happened to one attempt)."""

    policy_denied: bool = False
    transient_failure: bool = False
    user_constraint_missing: bool = False
    result_weak: bool = False
    grounded_and_valid: bool = False
    retry_budget_left: bool = True
    has_alternate_capability: bool = False


class ValidationSignals(_Contract):
    """Input to the CH02_03 validation table (is a produced result
    acceptable to return?)."""

    policy_compliance_failed: bool = False
    grounding_required_below_threshold: bool = False
    missing_required_fields: bool = False
    user_can_supply_fields: bool = True
    grounded_and_complete: bool = False
    partial_useful_low_risk: bool = False
    retry_budget_left: bool = True
    has_alternate_capability: bool = False


ActionResolver = Union[Decision, Callable[[Any], Decision]]
