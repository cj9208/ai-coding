"""Golden-set evaluation — the referee for every future plug-in decision.

Cases pin ``expected_substrings`` instead of chunk ids: the expectation is
resolved against the active snapshot at eval time, so a rebuild that
preserves text keeps passing (content-addressed ids) and a reviewer can
read the JSONL without knowing any internal id. A case with no expected
substrings is an abstention test: the pack must come back insufficient.

Metrics follow CH04's retrieval row: hit-rate@k, recall@k, precision@k,
abstention correctness; unresolved cases (expectations matching zero chunks)
are reported loudly, because a silently-empty expectation is this
harness's only own failure mode.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .contract import EvidencePack
from .engine import retrieve
from .store import RagStore


class GoldenCase(BaseModel):
    case_id: str
    question: str
    expected_substrings: list[str] = Field(default_factory=list)
    rationale: str = ""

    @property
    def expects_abstention(self) -> bool:
        return not self.expected_substrings


def load_cases(path: Path) -> list[GoldenCase]:
    cases = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(GoldenCase.model_validate(json.loads(line)))
    return cases


def evaluate(
    store: RagStore,
    cases: list[GoldenCase],
    k: int = 5,
    retrieve_fn: Any = None,
) -> dict[str, Any]:
    retrieve_fn = retrieve_fn or (lambda q: retrieve(store, q, k))
    version = store.active_version()
    rows = []
    for case in cases:
        pack: EvidencePack = retrieve_fn(case.question)
        if case.expects_abstention:
            rows.append(
                {
                    "case_id": case.case_id,
                    "kind": "abstention",
                    "passed": pack.insufficient,
                }
            )
            continue
        expected = (
            store.chunk_ids_matching(version, case.expected_substrings)
            if version
            else set()
        )
        if not expected:
            rows.append(
                {"case_id": case.case_id, "kind": "unresolved", "passed": False}
            )
            continue
        returned = [c.chunk_id for c in pack.chunks]
        hits = {r for r in returned if r in expected}
        rows.append(
            {
                "case_id": case.case_id,
                "kind": "retrieval",
                "passed": bool(hits),
                "recall": len(hits) / len(expected),
                "precision": len(hits) / k,
                "n_expected": len(expected),
            }
        )

    retrieval = [r for r in rows if r["kind"] == "retrieval"]
    abstentions = [r for r in rows if r["kind"] == "abstention"]
    unresolved = [r for r in rows if r["kind"] == "unresolved"]
    summary: dict[str, Any] = {
        "n_cases": len(rows),
        "hit_rate": _mean(r["passed"] for r in retrieval),
        "recall": _mean(r["recall"] for r in retrieval),
        "precision": _mean(r["precision"] for r in retrieval),
        "abstention_correct": (
            f"{sum(1 for r in abstentions if r['passed'])}/{len(abstentions)}"
            if abstentions
            else None
        ),
        "unresolved_case_ids": [r["case_id"] for r in unresolved],
        "per_case": rows,
    }
    return summary


def _mean(values: Any) -> float | None:
    vals = list(values)
    return round(sum(vals) / len(vals), 3) if vals else None
