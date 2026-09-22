"""Golden-case runner — CH04's regression strategy made executable:
compare *emitted decision objects*, never prompt internals.

Every case is fully scripted (FakeFrontHalf turns + capability results), so
the whole suite runs with zero LLM calls and zero network. The comparison is
threefold, as the design's "golden regression" promises:
expected final status/outcome, and the ordered list of decision-table rows
that must have fired (a subsequence check — extra intermediate rows are
allowed, missing ones are not).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .capabilities.fake import EchoCapability, FakeFrontHalf, ScriptedCapability
from .contracts import (
    CapabilityCatalogEntry,
    ExecutionBudget,
    GoldenCase,
    GoldenResult,
    OutcomeType,
    OutputContract,
    RequestStatus,
)
from .registry import HUMAN_HANDOFF, Registry
from .runtime import Orchestrator
from .store import Store

_HUMAN_ENTRY = CapabilityCatalogEntry(
    name=HUMAN_HANDOFF,
    owner="platform",
    domain_scope="global",
    capability_version="0.1.0",
    rollout_status="active",
    use_when=["terminal escalation"],
    avoid_when=[""],
    tool_schema_bundle=[],
    output_contract=OutputContract(required_fields=["handoff_id"]),
    fallbacks=[],
)


def registry_for(case: GoldenCase) -> Registry:
    """Build the one/two-capability registry a case needs. The primary is
    always exposed under ``case.capability`` (default "echo"); scripted
    ``results`` replace it with a pop-per-call script."""
    primary_name = case.capability or "echo"
    primary_impl = (
        ScriptedCapability(case.results) if case.results else EchoCapability()
    )
    entries: dict[str, CapabilityCatalogEntry] = {}
    impls: dict[str, Any] = {}
    primary = CapabilityCatalogEntry(
        name=primary_name,
        owner="golden",
        domain_scope="global",
        capability_version="0.0.0",
        rollout_status="active",
        task_types_supported=["*"],
        use_when=["golden scripting"],
        avoid_when=[""],
        tool_schema_bundle=[primary_name],
        output_contract=OutputContract(required_fields=list(case.required_fields)),
        validation_rules=list(case.validation_rules),
        fallbacks=[case.fallback_name] if case.fallback_name else [HUMAN_HANDOFF],
    )
    entries[primary_name] = primary
    impls[primary_name] = primary_impl
    if case.fallback_name:
        fb = CapabilityCatalogEntry(
            name=case.fallback_name,
            owner="golden",
            domain_scope="global",
            capability_version="0.0.0",
            rollout_status="active",
            task_types_supported=[case.fallback_name],
            use_when=["switch target in golden cases"],
            avoid_when=[""],
            tool_schema_bundle=[case.fallback_name],
            output_contract=OutputContract(
                required_fields=list(case.fallback_required_fields)
            ),
        )
        entries[case.fallback_name] = fb
        impls[case.fallback_name] = EchoCapability()
    entries[HUMAN_HANDOFF] = _HUMAN_ENTRY
    return Registry(entries=entries, impls=impls)


def run_case(case: GoldenCase, db_path: str | Path) -> GoldenResult:
    store = Store(db_path)
    try:
        orch = Orchestrator(store, registry_for(case), FakeFrontHalf(case.turns))
        budget = ExecutionBudget(**case.budget) if case.budget else None
        result = orch.run_turn(case.input, budget=budget)
        for answer in case.resume:
            if result.status != RequestStatus.awaiting_clarification:
                break
            result = orch.resume(result.request_id, answer)
        envelope = store.get_request(result.request_id)
        assert envelope is not None
        fired = _fired_row_ids(store, result.request_id)
        outcome = _final_outcome(
            store, result.request_id, envelope.state.current_status
        )

        diffs: list[str] = []
        exp = case.expected
        if envelope.state.current_status != exp.status:
            diffs.append(
                f"status: expected {exp.status.value},"
                f" got {envelope.state.current_status.value}"
            )
        if exp.outcome is not None and outcome != exp.outcome:
            diffs.append(f"outcome: expected {exp.outcome.value}, got {outcome}")
        missing = _missing_subsequence(exp.fired_row_ids, fired)
        if missing:
            diffs.append(f"rows not fired: {missing} (fired: {fired})")
        return GoldenResult(
            case_id=case.case_id,
            ok=not diffs,
            diffs=diffs,
            fired_row_ids=fired,
            status=envelope.state.current_status,
            outcome=outcome,
        )
    finally:
        store.close()


def run_cases(
    cases: list[GoldenCase], db_path: str | Path | None = None
) -> list[GoldenResult]:
    db_path = Path(db_path) if db_path else Path(__file__).with_name("_golden.tmp.db")
    results = []
    try:
        for case in cases:
            results.append(run_case(case, db_path))
    finally:
        if db_path.name == "_golden.tmp.db" and db_path.exists():
            db_path.unlink()
    return results


def load_cases(path: str | Path) -> list[GoldenCase]:
    cases: list[GoldenCase] = []
    for lineno, line in enumerate(
        Path(path).read_text(encoding="utf-8").splitlines(), 1
    ):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            cases.append(GoldenCase.model_validate(json.loads(line)))
        except (json.JSONDecodeError, ValidationError) as e:
            raise ValueError(f"{path}:{lineno}: bad golden case: {e}") from e
    return cases


# -- helpers -----------------------------------------------------------------
def _fired_row_ids(store: Store, request_id: str) -> list[str]:
    """Row ids in firing order: routing rows from decisions, validation/
    execution rows from the validation_completed event."""
    fired: list[str] = []
    for obj in store.objects(request_id):
        if obj["kind"] == "routing":
            fired.append(str(obj["payload"]["decision_reason"]["table_row_id"]))
    for ev in store.events(request_id):
        if ev["event"] == "validation_completed":
            p = ev["payload"]
            exec_row = p.get("execution_row_id")
            val_row = p.get("validation_row_id")
            if exec_row and val_row:
                fired.extend([exec_row, val_row])
            else:
                fired.append(str(p["row_id"]))
    return fired


def _final_outcome(
    store: Store, request_id: str, status: RequestStatus
) -> OutcomeType | None:
    last: OutcomeType | None = None
    for obj in store.objects(request_id):
        if obj["kind"] == "outcome":
            last = OutcomeType(obj["payload"]["outcome_type"])
    if last is not None:
        return last
    # terminals that persist their own object instead of an outcome row
    return {
        RequestStatus.awaiting_clarification: OutcomeType.clarification_requested,
        RequestStatus.handoff: OutcomeType.human_handoff,
        RequestStatus.rejected: OutcomeType.rejected,
        RequestStatus.failed: OutcomeType.failed,
    }.get(status)


def _missing_subsequence(expected: list[str], actual: list[str]) -> list[str]:
    """Order-preserving subset check; returns expected entries not consumed."""
    it = iter(actual)
    missing = []
    for row in expected:
        if not any(candidate == row for candidate in it):
            missing.append(row)
    return missing
