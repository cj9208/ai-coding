"""M3 lookup adapter: record translation against a fake backend + client
(zero LLM, no file_manager DB touched).
"""

from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from file_manager.search.base import SearchHit, SearchResult
from orchestrator.capabilities.lookup import StructuredLookupCapability
from orchestrator.contracts import CapabilityContext, ResultStatus


def _hit(i: int = 7) -> SearchHit:
    return SearchHit(
        id=i,
        filename=f"invoice_{i}.pdf",
        title="差旅发票",
        project_id=2,
        project_name="财务",
        uploader_name="张三",
        team_name="财务组",
        extension="pdf",
        size=1024,
        created_at=datetime(2026, 9, 1, 12, 0, 0),
        matched=["标题", "标签·模糊"],
    )


class _FakeClient:
    @contextmanager
    def session(self) -> Iterator[Any]:
        yield object()


class _FakeBackend:
    def __init__(
        self, result: SearchResult | None = None, exc: Exception | None = None
    ):
        self.result = result
        self.exc = exc
        self.queries: list[Any] = []

    def search(self, db: Any, query: Any) -> SearchResult:
        self.queries.append(query)
        if self.exc:
            raise self.exc
        assert self.result is not None
        return self.result


def _cap(
    result: SearchResult | None = None, exc: Exception | None = None
) -> tuple[StructuredLookupCapability, _FakeBackend]:
    backend = _FakeBackend(result, exc)
    return StructuredLookupCapability(client=_FakeClient(), backend=backend), backend


def _ctx(query: str = "发票 归档") -> CapabilityContext:
    return CapabilityContext(
        request_id="req_1",
        session_id="sess_1",
        normalized_query=query,
        task_type="file_lookup",
    )


# -- translation ---------------------------------------------------------------
def test_hits_map_to_success_with_records_and_refs() -> None:
    cap, _ = _cap(SearchResult(hits=[_hit(7), _hit(8)], total=2))
    result = cap.run(_ctx())
    assert result.status == ResultStatus.success
    assert result.output["result_count"] == 2
    assert [r["id"] for r in result.output["records"]] == [7, 8]
    assert result.evidence_refs == ["file_meta:7", "file_meta:8"]
    assert result.confidence_signals == {"record_count": 2.0}
    assert "共找到 2 条" in result.output["answer_markdown"]
    assert "差旅发票" in result.output["answer_markdown"]


def test_zero_hits_is_weak_insufficient_evidence() -> None:
    cap, _ = _cap(SearchResult(hits=[], total=0))
    result = cap.run(_ctx())
    assert result.status == ResultStatus.weak
    assert result.code == "insufficient_evidence"
    # still contract-shaped: the required fields are present so the
    # *tables*, not a missing-field gap, decide what happens next
    assert result.output["records"] == []
    assert "没有匹配" in result.output["answer_markdown"]


def test_relaxed_mode_is_reported_in_output() -> None:
    cap, _ = _cap(SearchResult(hits=[_hit()], total=1, relaxed=True))
    result = cap.run(_ctx())
    assert result.output["match_mode"] == "relaxed"


def test_empty_query_asks_the_user_not_the_db() -> None:
    cap, backend = _cap(SearchResult())
    result = cap.run(_ctx("   "))
    assert result.status == ResultStatus.weak
    assert result.code == "user_constraint_missing"
    assert result.output["clarification"]
    assert backend.queries == []  # the search was never attempted


def test_db_failure_is_structured_not_a_crash() -> None:
    cap, _ = _cap(exc=RuntimeError("unable to open database file"))
    result = cap.run(_ctx())
    assert result.status == ResultStatus.failed
    assert result.code == "dependency_unavailable"
    assert "unable to open" in result.output["error"]


# -- the query handed to the domain --------------------------------------------
def test_normalized_query_reaches_the_backend_verbatim() -> None:
    cap, backend = _cap(SearchResult(hits=[_hit()], total=1))
    cap.run(_ctx("报销 单号"))
    (sq,) = backend.queries
    assert sq.q == "报销 单号"
    assert sq.page_size == 10  # DEFAULT_LIMIT


def test_conservative_constraint_is_noop_for_exact_data() -> None:
    # DP-10 widens *retrieval of generated answers*; a record list has no
    # recall/precision tradeoff to manage, so the adapter ignores it —
    # assert that stays true if someone is tempted to "handle" it later
    cap, backend = _cap(SearchResult(hits=[_hit()], total=1))
    cap.run(
        CapabilityContext(
            request_id="r",
            session_id="s",
            normalized_query="发票",
            task_type="file_lookup",
            constraints={"conservative": True, "topk_factor": 1.5},
        )
    )
    (sq,) = backend.queries
    assert sq.page_size == 10
