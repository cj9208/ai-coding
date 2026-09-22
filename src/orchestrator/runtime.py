"""The deterministic core: state machine + control loop (CH02_02).

Invariants this module owns and nothing else may violate:
- it is the **sole writer** of ``envelope.state.current_status`` (every
  change goes through :meth:`Orchestrator._transition`, which refuses
  illegal edges and persists the envelope — DP-8);
- the 4-step control loop runs on every return to ``routing``:
  read latest result -> increment the right counter -> compare against the
  envelope budget -> let the tables decide only among *legal* actions
  (fallback rows supply the legality set);
- LLMs never appear here: the front half is an injected ``FrontHalf``,
  capabilities are an injected ``Registry`` — M0 scripts both (zero-LLM).

One ``run_turn``/``resume`` call = one CLI turn: the loop ends at a
non-terminal wait state (``awaiting_clarification``) or a terminal state;
budget state survives the process boundary because it lives on the
persisted envelope (DP-8).

Two entry faces, one loop (05a step 5): ``run_turn_async``/
``resume_async`` are awaitable from an ASGI/queue-worker host — the loop
body is unchanged, only its two slow points are awaited — and the sync
``run_turn``/``resume`` are boundary shims for the CLI, the golden suite
and the bench (``asyncio.run`` lives only here, never inside a turn).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import assess, policy
from .capabilities.builtin import build_handoff_packet
from .config import Lifecycle, Thresholds
from .contracts import (
    AttemptCounters,
    CapabilityContext,
    CapabilityExecutionRecord,
    CapabilityResult,
    CapReached,
    Decision,
    DecisionReason,
    ExecutionResult,
    ExecutionSignals,
    FinalOutcome,
    FrontHalfOutput,
    NextAction,
    OutcomeType,
    PolicyCheck,
    RequestEnvelope,
    RequestStatus,
    ResultStatus,
    RiskLevel,
    RoutingDecision,
    TurnResult,
    ValidationSignals,
    ValidationSummary,
)
from .ids import new_id
from .registry import Registry
from .store import Store

TERMINAL = {
    RequestStatus.completed,
    RequestStatus.handoff,
    RequestStatus.rejected,
    RequestStatus.failed,
}

#: legal state-machine edges (CH02_02 diagram; the handoff->routing recovery
#: edge is intentionally absent in v1 — a handoff ends the session's turn)
LEGAL_TRANSITIONS: dict[RequestStatus, frozenset[RequestStatus]] = {
    RequestStatus.captured: frozenset(
        {RequestStatus.interpreting, RequestStatus.failed}
    ),
    RequestStatus.interpreting: frozenset(
        {RequestStatus.routing, RequestStatus.failed}
    ),
    RequestStatus.routing: frozenset(
        {
            RequestStatus.executing,
            RequestStatus.interpreting,  # stronger_model re-interpret
            RequestStatus.awaiting_clarification,
            RequestStatus.handoff,
            RequestStatus.rejected,
            RequestStatus.failed,
        }
    ),
    RequestStatus.awaiting_clarification: frozenset({RequestStatus.interpreting}),
    RequestStatus.executing: frozenset({RequestStatus.validating}),
    RequestStatus.validating: frozenset(
        {
            RequestStatus.routing,
            RequestStatus.executing,  # retry / switch re-enter execution
            RequestStatus.awaiting_clarification,
            RequestStatus.completed,
            RequestStatus.handoff,
            RequestStatus.rejected,
            RequestStatus.failed,
        }
    ),
    RequestStatus.completed: frozenset(),
    RequestStatus.handoff: frozenset(),
    RequestStatus.rejected: frozenset(),
    RequestStatus.failed: frozenset(),
}

TRANSIENT_CODES = {"transient", "dependency_timeout", "capability_crashed"}


def _now_ms() -> int:
    return int(time.time() * 1000)


def _refuse_inside_loop(sync_entry: str, async_entry: str) -> None:
    """Checked before the coroutine is built: an aborted shim must not
    also leak a 'never awaited' warning on top of its clear error."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(
        f"{sync_entry}() is the sync entry point; inside a running event"
        f" loop await {async_entry}() instead"
    )


@dataclass
class _Turn:
    """Mutable per-turn scratch; the envelope stays the only *durable*
    state (DP-8), this only avoids re-reading the store within one call."""

    out: FrontHalfOutput | None = None
    last_result: CapabilityResult | None = None
    constraints: dict[str, Any] = field(default_factory=dict)
    escalated: bool = False
    pending_answer: str | None = None
    decision_ids: list[str] = field(default_factory=list)
    used_execution_ids: list[str] = field(default_factory=list)
    fallback_history: list[str] = field(default_factory=list)
    outcome_type: OutcomeType | None = None


class Orchestrator:
    def __init__(
        self,
        store: Store,
        registry: Registry,
        front_half: Any,
        *,
        now_ms: Callable[[], int] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry
        self.front_half = front_half
        self._now = now_ms or _now_ms

    # -- public entry points --------------------------------------------------
    def run_turn(
        self,
        text: str,
        *,
        user_id: str = "cli_user",
        session_id: str | None = None,
        locale: str = "zh",
        budget: Any = None,
    ) -> TurnResult:
        """Sync shim: CLI, golden suite, bench. Fails fast inside a
        running loop — async hosts call :meth:`run_turn_async`."""
        _refuse_inside_loop("run_turn", "run_turn_async")
        return asyncio.run(
            self.run_turn_async(
                text,
                user_id=user_id,
                session_id=session_id,
                locale=locale,
                budget=budget,
            )
        )

    async def run_turn_async(
        self,
        text: str,
        *,
        user_id: str = "cli_user",
        session_id: str | None = None,
        locale: str = "zh",
        budget: Any = None,
    ) -> TurnResult:
        envelope = RequestEnvelope.new(
            text=text,
            user_id=user_id,
            session_id=session_id,
            locale=locale,
            budget=budget,
        )
        with self.store.transact():
            self.store.create_request(envelope)
            self.store.emit(
                envelope.request_id, "request_captured", self._now(), text=text
            )
        turn = _Turn()
        return await self._drive(envelope, turn)

    def resume(self, request_id: str, answer: str) -> TurnResult:
        """Sync shim, same contract as :meth:`run_turn`."""
        _refuse_inside_loop("resume", "resume_async")
        return asyncio.run(self.resume_async(request_id, answer))

    async def resume_async(self, request_id: str, answer: str) -> TurnResult:
        envelope = self.store.get_request(request_id)
        if envelope is None:
            raise ValueError(f"unknown request: {request_id}")
        if envelope.state.current_status != RequestStatus.awaiting_clarification:
            raise ValueError(
                f"request {request_id} is {envelope.state.current_status.value},"
                " not awaiting clarification"
            )
        now = self._now()
        gap_ms = now - (envelope.state.wait_started_ms or now)
        expired = gap_ms > Lifecycle.CLARIFICATION_TTL_MS
        with self.store.transact():
            if expired:
                self._expire_stale_wait(envelope, now, gap_ms)
            self._append(envelope, "clarification_answer", {"answer": answer}, now)
            self.store.emit(
                request_id,
                "clarification_received",
                now,
                answer=answer,
                **({"expired_ttl_ms": gap_ms} if expired else {}),
            )
            self._transition(envelope, RequestStatus.interpreting)
        turn = _Turn(pending_answer=answer)
        return await self._drive(envelope, turn)

    def _expire_stale_wait(
        self, envelope: RequestEnvelope, now: int, gap_ms: int
    ) -> None:
        """A wait past the TTL ends the interpretation it would continue.

        DP-4 subtracts human latency out of the wall clock, which is right
        for machine budgets and leaves one side effect: a request from six
        months ago is still resumable, against a registry, alias table and
        corpus that have moved on. So the resumed turn re-derives
        capability selection and gets a fresh *machine* budget, while
        ``clarification_turns`` — a lifecycle bound, not a cost bound —
        survives. The event keeps the gap auditable (DP-7).
        """
        envelope.attempt_counters = AttemptCounters(
            clarification_turns=envelope.attempt_counters.clarification_turns
        )
        envelope.state.selected_capability = None
        envelope.state.selected_domain = None
        self.store.emit(
            envelope.request_id,
            "clarification_expired",
            now,
            gap_ms=gap_ms,
            ttl_ms=Lifecycle.CLARIFICATION_TTL_MS,
        )

    # -- the loop --------------------------------------------------------------
    async def _drive(self, envelope: RequestEnvelope, turn: _Turn) -> TurnResult:
        """One loop pass = one commit, except where the turn has to wait on
        something slow (the front half, a capability) — those stages keep
        their writes outside the call and group them with ``transact()``.
        Holding the SQLite write lock across an LLM call would turn a
        batching win into a contention loss.  The two slow points are
        ``await``ed (05a step 5); every decision-table step in between
        stays synchronous, so the loop's structure is what it always was."""
        while True:
            now = self._now()
            if assess.wall_clock_exceeded(envelope, now):
                return self._finish_failed(envelope, turn, "wall_clock_exceeded")
            status = envelope.state.current_status
            if status == RequestStatus.captured:
                with self.store.transact():
                    self._transition(envelope, RequestStatus.interpreting)
            elif status == RequestStatus.interpreting:
                await self._interpret(envelope, turn)
            elif status == RequestStatus.routing:
                with self.store.transact():
                    result = self._route(envelope, turn, now)
                if result is not None:
                    return result
            elif status == RequestStatus.executing:
                await self._execute(envelope, turn)
            elif status == RequestStatus.validating:
                with self.store.transact():
                    result = self._validate(envelope, turn)
                if result is not None:
                    return result
            else:
                return self._turn_result(envelope, turn)

    def _append(
        self, envelope: RequestEnvelope, kind: str, payload: Any, now_ms: int
    ) -> str:
        """Append one object, taking ``seq`` from the envelope counter
        (DP-8: per-request state lives on the envelope, not in a
        read-then-write ``MAX(seq)+1`` query)."""
        seq = envelope.state.next_seq
        envelope.state.next_seq = seq + 1
        return self.store.append_object(envelope.request_id, kind, payload, now_ms, seq)

    async def _interpret(self, envelope: RequestEnvelope, turn: _Turn) -> None:
        # the model call is awaited *before* any write, so no lock is held
        # across it; the three writes after it group into one commit
        out = await self.front_half.interpret(
            envelope, answer=turn.pending_answer, escalated=turn.escalated
        )
        turn.pending_answer = None
        turn.escalated = False
        turn.out = out
        now = self._now()
        with self.store.transact():
            self._append(envelope, "interpretation", out.interpretation, now)
            self.store.emit(
                envelope.request_id,
                "interpretation_created",
                now,
                interpretation_id=out.interpretation.interpretation_id,
                task_type=out.interpretation.task_type,
            )
            self._transition(envelope, RequestStatus.routing)

    def _route(
        self, envelope: RequestEnvelope, turn: _Turn, now: int
    ) -> TurnResult | None:
        assert turn.out is not None, "routing without an interpretation"
        # control-loop step 2: this pass consumes one loop
        envelope.attempt_counters.total_loops += 1
        caps = assess.caps_reached(envelope, now)
        assessment, signals = assess.routing_signals(envelope, turn.out, caps)

        row = policy.ROUTING_TABLE.decide(signals)
        decision, row_id = row.action, row.row_id
        # control-loop step 3/4: budget legality -> fallback table override
        decision, override = self._apply_fallbacks(envelope, caps, decision)
        if override:
            turn.fallback_history.append(override)
            row_id = override

        capability: str | None = None
        domain: str | None = None
        constraints = dict(turn.out.constraints)
        if decision in (
            Decision.proceed,
            Decision.proceed_conservative,
            Decision.execute_capability,
        ):
            capability = self.registry.select(turn.out.interpretation.task_type)
            if capability is None:
                decision = Decision.handoff_human
                row_id = (
                    f"fallback:no_capability_for:"
                    f"{turn.out.interpretation.task_type}"
                )
            else:
                domain = self.registry.entry(capability).domain_scope
                if decision == Decision.proceed_conservative:  # DP-10
                    constraints["conservative"] = True
                    constraints["topk_factor"] = Thresholds.CONSERVATIVE_TOPK_FACTOR
        turn.constraints = constraints

        payload: dict[str, Any] = {}
        if decision == Decision.clarify:
            payload["question"] = turn.out.clarification_question
        if constraints:
            payload["constraints"] = constraints
        rd = RoutingDecision(
            routing_decision_id=new_id("route"),
            request_id=envelope.request_id,
            interpretation_id=turn.out.interpretation.interpretation_id,
            timestamp_ms=now,
            decision=decision,
            decision_reason=DecisionReason(
                primary=row.reason,
                supporting_signals=[assessment.rationale_primary]
                + list(assessment.supporting_signals),
                table_row_id=row_id,
            ),
            selected_domain=domain,
            selected_capability=capability,
            fallback_capability=(
                self.registry.fallback_for(capability) if capability else None
            ),
            constraints=constraints,
            next_action=NextAction(type=decision.value, payload=payload),
        )
        turn.decision_ids.append(rd.routing_decision_id)
        self._append(envelope, "routing", rd, now)
        self.store.emit(
            envelope.request_id,
            "routing_decided",
            now,
            decision=rd.decision.value,
            row_id=row_id,
            routing_decision_id=rd.routing_decision_id,
        )

        if decision in (
            Decision.proceed,
            Decision.proceed_conservative,
            Decision.execute_capability,
        ):
            envelope.state.selected_capability = capability
            envelope.state.selected_domain = domain
            self._transition(envelope, RequestStatus.executing)
            return None
        if decision == Decision.clarify:
            envelope.attempt_counters.clarification_turns += 1
            self._transition(envelope, RequestStatus.awaiting_clarification)
            self.store.emit(
                envelope.request_id,
                "clarification_requested",
                now,
                question=turn.out.clarification_question,
            )
            return self._turn_result(envelope, turn)
        if decision == Decision.stronger_model:
            envelope.attempt_counters.model_escalations += 1
            envelope.attempt_counters.reinterpretations += 1
            turn.escalated = True
            self._transition(envelope, RequestStatus.interpreting)
            return None
        if decision == Decision.reject:
            return self._finish_rejected(envelope, turn, rd.decision_reason.primary)
        # handoff_human (r2 / r9 / fallback override)
        return self._finish_handoff(envelope, turn, rd.decision_reason.primary)

    async def _execute(self, envelope: RequestEnvelope, turn: _Turn) -> None:
        assert turn.out is not None and envelope.state.selected_capability
        name = envelope.state.selected_capability
        entry = self.registry.entry(name)
        impl = self.registry.impl(name)
        routing_id = turn.decision_ids[-1] if turn.decision_ids else ""
        ctx = CapabilityContext(
            request_id=envelope.request_id,
            session_id=envelope.session_id,
            normalized_query=turn.out.interpretation.normalized_query,
            task_type=turn.out.interpretation.task_type,
            requested_attributes=list(turn.out.interpretation.requested_attributes),
            policy_context=envelope.policy_context,
            constraints=dict(turn.constraints),
        )
        now = self._now()
        self.store.emit(
            envelope.request_id, "capability_execution_started", now, capability=name
        )
        try:
            # awaited outside any transact(): the write lock is never held
            # across the capability call itself
            result = await impl.run(ctx)
        except Exception:  # a crash is a structured failure, never a re-raise
            result = CapabilityResult(
                status=ResultStatus.failed, code="capability_crashed", output={}
            )
        end = self._now()
        denied = result.code == "permission_denied"
        rec = CapabilityExecutionRecord(
            execution_id=new_id("exec"),
            request_id=envelope.request_id,
            routing_decision_id=routing_id,
            timestamp_start_ms=now,
            timestamp_end_ms=end,
            domain=entry.domain_scope,
            capability_name=name,
            capability_version=entry.capability_version,
            tool_bundle_loaded=list(entry.tool_schema_bundle),
            policy_check=PolicyCheck(
                allowed=not denied,
                permission_profile=envelope.policy_context.permission_profile,
                risk_decision="denied" if denied else "approved",
            ),
            result=ExecutionResult(
                status=result.status,
                output=result.output,
                evidence_refs=list(result.evidence_refs),
                confidence_signals=dict(result.confidence_signals),
            ),
            execution_plan=list(result.tool_steps),  # observational — DP-6
            duration_ms=end - now,
        )
        turn.last_result = result
        envelope.attempt_counters.tool_calls += len(result.tool_steps)
        with self.store.transact():
            self._append(envelope, "execution", rec, end)
            self.store.emit(
                envelope.request_id,
                "capability_execution_completed",
                end,
                execution_id=rec.execution_id,
                status=result.status.value,
                code=result.code,
                duration_ms=rec.duration_ms,
            )
            self._transition(envelope, RequestStatus.validating)

    def _validate(self, envelope: RequestEnvelope, turn: _Turn) -> TurnResult | None:
        assert turn.last_result is not None and envelope.state.selected_capability
        now = self._now()
        name = envelope.state.selected_capability
        entry = self.registry.entry(name)
        result = turn.last_result

        user_constraint_missing = result.code == "user_constraint_missing"
        exec_signals = ExecutionSignals(
            policy_denied=result.code == "permission_denied",
            transient_failure=(
                result.status == ResultStatus.failed
                and (result.code or "") in TRANSIENT_CODES
            ),
            user_constraint_missing=user_constraint_missing,
            # a missing *user constraint* is not generic weakness: switching
            # capabilities cannot fix it, so it must not fire e4 before e5
            result_weak=result.status == ResultStatus.weak
            and not user_constraint_missing,
            grounded_and_valid=result.status == ResultStatus.success,
            retry_budget_left=assess.retry_budget_left(envelope),
            has_alternate_capability=self.registry.fallback_for(name) is not None,
        )
        exec_row = policy.EXECUTION_TABLE.decide(exec_signals)
        action = exec_row.action
        row_id = exec_row.row_id
        val_row_id = ""
        validation = ValidationSummary()

        if action == Decision.accept:
            val_signals = _validation_signals(envelope, entry, result, self.registry)
            val_row = policy.VALIDATION_TABLE.decide(val_signals)
            action = val_row.action
            row_id = val_row.row_id
            val_row_id = val_row.row_id
            validation = ValidationSummary(
                grounding=(
                    "fail" if val_signals.grounding_required_below_threshold else "pass"
                ),
                completeness="fail" if val_signals.missing_required_fields else "pass",
                policy_compliance=(
                    "fail" if val_signals.policy_compliance_failed else "pass"
                ),
            )

        self.store.emit(
            envelope.request_id,
            "validation_completed",
            now,
            row_id=row_id,
            execution_row_id=exec_row.row_id,
            validation_row_id=val_row_id,
            action=action.value,
        )
        caps = assess.caps_reached(envelope, now)
        action, override = self._apply_fallbacks(envelope, caps, action)
        if override:
            turn.fallback_history.append(override)

        if action == Decision.accept:
            return self._finish_completed(
                envelope, turn, OutcomeType.answered, validation, _answer_text(result)
            )
        if action == Decision.partial_answer:
            return self._finish_completed(
                envelope,
                turn,
                OutcomeType.partial_answer,
                validation,
                _answer_text(result),
            )
        if action == Decision.retry:
            envelope.attempt_counters.execution_retries += 1
            self._transition(envelope, RequestStatus.executing)
            return None
        if action == Decision.switch_capability:
            alt = self.registry.fallback_for(name)
            if alt is None:  # table said switch but nothing exists -> handoff
                return self._finish_handoff(envelope, turn, row_id)
            envelope.state.selected_capability = alt
            envelope.state.selected_domain = self.registry.entry(alt).domain_scope
            self._transition(envelope, RequestStatus.executing)
            return None
        if action == Decision.clarify:
            envelope.attempt_counters.clarification_turns += 1
            self._transition(envelope, RequestStatus.awaiting_clarification)
            if turn.out and not turn.out.clarification_question:
                # the execution path (e5) asks what the *capability* found
                # missing, not what the front half guessed
                turn.out.clarification_question = str(
                    result.output.get("clarification") or ""
                )
            question = (turn.out.clarification_question if turn.out else "") or (
                "请补充所需信息。"
            )
            self.store.emit(
                envelope.request_id, "clarification_requested", now, question=question
            )
            return self._turn_result(envelope, turn)
        if action == Decision.reject:
            return self._finish_rejected(envelope, turn, row_id)
        if action == Decision.failed:
            return self._finish_failed(envelope, turn, row_id)
        return self._finish_handoff(envelope, turn, row_id)

    # -- legality (CH02_02 fallback table) --------------------------------------
    #: which spent cap blocks which branch's action (the cap counts the
    #: consumption of *that* action; unrelated actions stay legal)
    _CAP_BLOCKS: dict[Decision, set[CapReached]] = {
        Decision.stronger_model: {
            CapReached.model_escalation_cap,
            CapReached.reinterpretation_cap,
        },
        Decision.clarify: {CapReached.clarification_cap},
        Decision.retry: {CapReached.execution_retry_cap},
    }

    def _apply_fallbacks(
        self,
        envelope: RequestEnvelope,
        caps: list[CapReached],
        decision: Decision,
    ) -> tuple[Decision, str | None]:
        """A spent cap forbids only the action that would consume it; the
        blockage is resolved through the fallback table (preferred action
        first, then the cap's other legal actions). handoff/reject/accept
        are never blocked, so the walk always terminates."""
        if not caps:
            return decision, None
        spent = set(caps)

        def blocked(d: Decision) -> bool:
            return d in self._CAP_BLOCKS and bool(self._CAP_BLOCKS[d] & spent)

        if not blocked(decision):
            return decision, None
        for cap in caps:
            row = policy.FALLBACK_ROWS[cap]
            for candidate in [row.preferred] + [
                a for a in sorted(row.allowed, key=str) if a != row.preferred
            ]:
                if not blocked(candidate) and candidate != decision:
                    return candidate, f"fallback:{cap.value}:{candidate.value}"
        return Decision.handoff_human, "fallback:no_legal_action"

    # -- terminal helpers -------------------------------------------------------
    def _finish_completed(
        self,
        envelope: RequestEnvelope,
        turn: _Turn,
        outcome_type: OutcomeType,
        validation: ValidationSummary,
        response: str,
    ) -> TurnResult:
        now = self._now()
        turn.outcome_type = outcome_type
        fo = FinalOutcome(
            final_outcome_id=new_id("out"),
            request_id=envelope.request_id,
            timestamp_ms=now,
            outcome_type=outcome_type,
            user_response=response,
            validation_summary=validation,
            used_execution_ids=list(turn.used_execution_ids),
            fallback_history=list(turn.fallback_history),
        )
        self._append(envelope, "outcome", fo, now)
        envelope.state.final_outcome_ref = fo.final_outcome_id
        self._transition(envelope, RequestStatus.completed)
        self.store.emit(
            envelope.request_id,
            "request_completed",
            now,
            outcome_type=outcome_type.value,
        )
        return self._turn_result(envelope, turn, response=response)

    def _finish_rejected(
        self, envelope: RequestEnvelope, turn: _Turn, reason: str
    ) -> TurnResult:
        return self._finish_stop(
            envelope,
            turn,
            OutcomeType.rejected,
            reason,
            RequestStatus.rejected,
            "request_rejected",
        )

    def _finish_failed(
        self, envelope: RequestEnvelope, turn: _Turn, reason: str
    ) -> TurnResult:
        return self._finish_stop(
            envelope,
            turn,
            OutcomeType.failed,
            reason,
            RequestStatus.failed,
            "request_failed",
        )

    def _finish_handoff(
        self, envelope: RequestEnvelope, turn: _Turn, reason: str
    ) -> TurnResult:
        now = self._now()
        objects = self.store.objects(envelope.request_id)
        packet = build_handoff_packet(
            envelope,
            reason_code=reason,
            reason_summary=reason,
            objects=objects,
            now_ms=now,
        )
        self._append(envelope, "handoff", packet, now)
        self._transition(envelope, RequestStatus.handoff)
        self.store.emit(
            envelope.request_id,
            "human_handoff_created",
            now,
            handoff_id=packet.handoff_id,
            reason_code=reason,
        )
        turn.outcome_type = OutcomeType.human_handoff
        result = self._turn_result(envelope, turn)
        result.handoff_id = packet.handoff_id
        result.response = (
            f"已转人工处理（handoff {packet.handoff_id}）；"
            "用 `orchestrate handoff export` 导出交接单。"
        )
        return result

    def _finish_stop(
        self,
        envelope: RequestEnvelope,
        turn: _Turn,
        outcome_type: OutcomeType,
        reason: str,
        status: RequestStatus,
        event: str,
    ) -> TurnResult:
        now = self._now()
        turn.outcome_type = outcome_type
        fo = FinalOutcome(
            final_outcome_id=new_id("out"),
            request_id=envelope.request_id,
            timestamp_ms=now,
            outcome_type=outcome_type,
            user_response=reason,
        )
        self._append(envelope, "outcome", fo, now)
        envelope.state.final_outcome_ref = fo.final_outcome_id
        self._transition(envelope, status)
        self.store.emit(envelope.request_id, event, now, reason=reason)
        return self._turn_result(envelope, turn, response=reason)

    # -- plumbing ---------------------------------------------------------------
    def _transition(self, envelope: RequestEnvelope, to: RequestStatus) -> None:
        """Sole writer of current_status: guard the edge, persist."""
        current = envelope.state.current_status
        if to not in LEGAL_TRANSITIONS[current]:
            raise AssertionError(f"illegal transition: {current.value} -> {to.value}")
        now = self._now()
        if to == RequestStatus.awaiting_clarification:
            envelope.state.wait_started_ms = now
        elif (
            current == RequestStatus.awaiting_clarification
            and envelope.state.wait_started_ms is not None
        ):
            # waiting on a human is not the machine overspending its budget
            envelope.execution_budget.wall_clock_paused_ms += (
                now - envelope.state.wait_started_ms
            )
            envelope.state.wait_started_ms = None
        envelope.state.current_status = to
        self.store.update_request(envelope, now)

    def _turn_result(
        self, envelope: RequestEnvelope, turn: _Turn, response: str = ""
    ) -> TurnResult:
        status = envelope.state.current_status
        question = ""
        outcome_type = turn.outcome_type
        if status == RequestStatus.awaiting_clarification:
            question = (turn.out.clarification_question if turn.out else "") or (
                "请补充所需信息。"
            )
            outcome_type = OutcomeType.clarification_requested
        return TurnResult(
            request_id=envelope.request_id,
            status=status,
            outcome_type=outcome_type,
            response=response,
            question=question,
            routing_decision_ids=list(turn.decision_ids),
        )


def _answer_text(result: CapabilityResult) -> str:
    for key in ("answer_markdown", "text", "answer"):
        value = result.output.get(key)
        if isinstance(value, str) and value:
            return value
    return str(result.output)


ANSWER_KEYS = ("answer_markdown", "text", "answer")


def _has_usable_text(output: dict[str, Any]) -> bool:
    return any(
        isinstance(output.get(k), str) and output[k].strip() for k in ANSWER_KEYS
    )


def _validation_signals(
    envelope: RequestEnvelope,
    entry: Any,
    result: CapabilityResult,
    registry: Registry,
) -> ValidationSignals:
    required = entry.output_contract.required_fields
    missing = [f for f in required if f not in result.output]
    coverage = result.confidence_signals.get("grounding_coverage", 1.0)
    grounding_required = "grounding_coverage_min" in entry.validation_rules
    grounding_low = grounding_required and coverage < Thresholds.GROUNDING_COVERAGE_MIN
    has_text = result.status != ResultStatus.failed and _has_usable_text(result.output)
    return ValidationSignals(
        policy_compliance_failed=result.code == "policy_violation",
        grounding_required_below_threshold=grounding_low,
        missing_required_fields=bool(missing),
        # user can only supply what is missing if there is no usable text
        user_can_supply_fields=bool(missing) and not has_text,
        grounded_and_complete=(
            result.status == ResultStatus.success and not missing and not grounding_low
        ),
        partial_useful_low_risk=(
            has_text and bool(missing) and entry.risk_profile == RiskLevel.low
        ),
        retry_budget_left=assess.retry_budget_left(envelope),
        has_alternate_capability=registry.fallback_for(entry.name) is not None,
    )
