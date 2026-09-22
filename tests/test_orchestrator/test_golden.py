"""The checked-in golden file must be green, and — per DP-7 — its routing
rows must cover the whole routing table (exec/validation rows are covered
row-by-row in test_policy)."""

from pathlib import Path

from orchestrator.cli import DEFAULT_GOLDEN_FILE
from orchestrator.contracts import RequestStatus
from orchestrator.golden import load_cases, run_cases
from orchestrator.policy import ROUTING_TABLE

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES = REPO_ROOT / "config" / "orchestrator" / "golden_cases.jsonl"


def test_default_golden_file_location_matches_cli() -> None:
    assert DEFAULT_GOLDEN_FILE == CASES


def test_all_golden_cases_pass(tmp_path: Path) -> None:
    cases = load_cases(CASES)
    assert len(cases) >= 17
    results = run_cases(cases, db_path=tmp_path / "golden.db")
    failures = [f"{r.case_id}: {r.diffs}" for r in results if not r.ok]
    assert not failures


def test_golden_suite_covers_every_routing_row() -> None:
    cases = load_cases(CASES)
    fired = {row for case in cases for row in case.expected.fired_row_ids}
    missing = [r for r in ROUTING_TABLE.row_ids() if r not in fired]
    assert not missing


def test_clarify_cases_reactivate_through_resume(tmp_path: Path) -> None:
    cases = [c for c in load_cases(CASES) if c.resume]
    results = {r.case_id: r for r in run_cases(cases, db_path=tmp_path / "g.db")}
    assert cases, "expected at least one resume case"
    for case in cases:
        assert results[case.case_id].ok
        # a resumed case either completes or hands off; never stalls
        assert results[case.case_id].status in (
            RequestStatus.completed,
            RequestStatus.handoff,
        )
