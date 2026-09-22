"""Store: three-table persistence, append-only ordering, envelope as root."""

import json
from pathlib import Path
from typing import Iterator

import pydantic
import pytest

from orchestrator.contracts import RequestEnvelope, RequestStatus
from orchestrator.store import ConflictError, Store


@pytest.fixture()
def store(tmp_path: Path) -> Iterator[Store]:
    s = Store(tmp_path / "orch.db")
    yield s
    s.close()


def test_envelope_roundtrip_is_lossless(store: Store) -> None:
    env = RequestEnvelope.new(text="春晖省钱卡怎么用", user_id="u1")
    store.create_request(env)
    loaded = store.get_request(env.request_id)
    assert loaded is not None
    assert loaded.model_dump() == env.model_dump()


def test_unknown_request_returns_none(store: Store) -> None:
    assert store.get_request("req_nope") is None


def test_status_column_tracks_envelope_state(store: Store) -> None:
    env = RequestEnvelope.new(text="q")
    store.create_request(env)
    env.state.current_status = RequestStatus.routing
    store.update_request(env, now_ms=env.timestamp_start_ms + 5)
    rows = store.list_requests()
    assert rows[0]["status"] == "routing"
    assert rows[0]["request_id"] == env.request_id


def test_object_seq_is_global_per_request(store: Store) -> None:
    env = RequestEnvelope.new(text="q")
    store.create_request(env)
    now = env.timestamp_start_ms
    store.append_object(env.request_id, "interpretation", {"a": 1}, now)
    store.append_object(env.request_id, "routing", {"a": 2}, now)
    store.append_object(env.request_id, "interpretation", {"a": 3}, now)
    objects = store.objects(env.request_id)
    assert [o["seq"] for o in objects] == [1, 2, 3]
    assert [o["payload"]["a"] for o in objects] == [1, 2, 3]


def test_events_are_ordered_and_typed(store: Store) -> None:
    env = RequestEnvelope.new(text="q")
    store.create_request(env)
    now = env.timestamp_start_ms
    store.emit(env.request_id, "request_captured", now, text="q")
    store.emit(env.request_id, "routing_decided", now + 1, decision="proceed")
    events = store.events(env.request_id)
    assert [e["event"] for e in events] == ["request_captured", "routing_decided"]
    assert events[1]["payload"] == {"decision": "proceed"}


def test_objects_of_kind_finds_handoffs_across_requests(store: Store) -> None:
    for _ in range(2):
        env = RequestEnvelope.new(text="q")
        store.create_request(env)
        store.append_object(env.request_id, "handoff", {"x": 1}, env.timestamp_start_ms)
    rows = store.objects_of_kind("handoff")
    assert len(rows) == 2
    assert store.objects_of_kind("nope") == []


def test_cross_request_reads_are_indexed(store: Store) -> None:
    """Both cross-request reads sorted a whole table per call without these
    (measured by ``scripts/orch_bench_run.py``: ``list_requests`` P50 188 ms
    at 10^5 requests)."""
    from sqlalchemy import inspect

    engine = store._client.engine
    req_cols = {
        tuple(i["column_names"]) for i in inspect(engine).get_indexes("requests")
    }
    obj_cols = {
        tuple(i["column_names"]) for i in inspect(engine).get_indexes("runtime_objects")
    }
    assert ("updated_at_ms",) in req_cols
    assert ("kind", "created_at_ms") in obj_cols


def test_update_request_is_a_compare_and_set(store: Store) -> None:
    """Two readers of the same envelope must not both write: the loser gets
    a typed ConflictError instead of silently overwriting the winner's
    counters (docs/orchestrator/04-scaling.md §2)."""
    env = RequestEnvelope.new(text="q")
    store.create_request(env)
    winner = store.get_request(env.request_id)
    loser = store.get_request(env.request_id)
    assert winner and loser

    store.update_request(winner, now_ms=env.timestamp_start_ms + 10)
    assert winner.state.version == 2
    with pytest.raises(ConflictError) as exc:
        store.update_request(loser, now_ms=env.timestamp_start_ms + 20)
    assert exc.value.request_id == env.request_id
    assert exc.value.expected_version == 1
    # the loser's in-memory copy is left consistent with what it believes
    assert loser.state.version == 1
    # and the row still reflects only the winner
    assert store.get_request(env.request_id).state.version == 2  # type: ignore[union-attr]


def test_transact_commits_and_rolls_back_as_a_unit(store: Store) -> None:
    env = RequestEnvelope.new(text="q")
    with store.transact():
        store.create_request(env)
        store.emit(env.request_id, "request_captured", env.timestamp_start_ms, text="q")
        store.append_object(env.request_id, "routing", {"a": 1}, env.timestamp_start_ms)
    assert store.get_request(env.request_id) is not None

    other = RequestEnvelope.new(text="q2")
    with pytest.raises(RuntimeError):
        with store.transact():
            store.create_request(other)
            store.emit(other.request_id, "request_captured", 0, text="q2")
            raise RuntimeError("pass died before committing")
    assert store.get_request(other.request_id) is None
    assert store.events(other.request_id) == []


def test_reads_inside_a_transact_see_its_own_writes(store: Store) -> None:
    """A loop pass reads back objects it appended (the handoff packet does);
    uncommitted-but-same-transaction must be visible."""
    env = RequestEnvelope.new(text="q")
    with store.transact():
        store.create_request(env)
        store.append_object(env.request_id, "handoff", {"x": 1}, env.timestamp_start_ms)
        assert store.objects(env.request_id) != []
    assert store.objects_of_kind("handoff") != []


def test_original_input_is_write_once() -> None:
    env = RequestEnvelope.new(text="q")
    with pytest.raises(pydantic.ValidationError):
        # setattr, not attribute syntax: the assignment must *run* for the
        # model to reject it — a static checker has no business here
        setattr(env.original_input, "text", "rewritten")


def test_unknown_json_fields_are_ignored_on_load(store: Store) -> None:
    # forward-compatible re-read: simulate a newer writer adding a field
    raw = RequestEnvelope.new(text="q").model_dump()
    raw["future_field"] = 42
    loaded = RequestEnvelope.model_validate(raw)
    assert loaded.original_input.text == "q"


def _seed(
    store: Store, user_id: str, created_at_ms: int, calls: int
) -> RequestEnvelope:
    env = RequestEnvelope.new(text="q", user_id=user_id)
    env.timestamp_start_ms = created_at_ms
    env.attempt_counters.llm_calls = calls
    store.create_request(env)
    return env


def test_llm_calls_today_sums_the_users_utc_day(store: Store) -> None:
    from orchestrator.store import DAY_MS

    now = 1_758_000_000_000  # any ms instant inside a day
    day_start = now - (now % DAY_MS)
    _seed(store, "u1", day_start + 1_000, 2)  # early today
    _seed(store, "u1", now, 3)  # later today
    _seed(store, "u1", day_start - 1, 9)  # last day's last ms
    _seed(store, "u2", now, 100)  # another user
    assert store.llm_calls_today("u1", now) == 5
    assert store.llm_calls_today("u2", now) == 100
    # next day: today's rows fall out of the window
    assert store.llm_calls_today("u1", day_start + DAY_MS) == 0


def test_llm_calls_today_can_exclude_the_in_flight_request(store: Store) -> None:
    now = 1_758_000_000_000
    mine = _seed(store, "u1", now, 4)
    _seed(store, "u1", now, 3)
    assert store.llm_calls_today("u1", now) == 7
    assert store.llm_calls_today("u1", now, exclude_request_id=mine.request_id) == 3


def test_llm_calls_today_reads_legacy_envelopes_as_zero(store: Store) -> None:
    """Rows written before the counter existed return NULL from
    json_extract and simply do not add."""
    from sqlalchemy import text

    now = 1_758_000_000_000
    env = _seed(store, "u1", now, 2)
    legacy = RequestEnvelope.new(text="q", user_id="u1")
    legacy.timestamp_start_ms = now
    raw = legacy.model_dump()
    del raw["attempt_counters"]["llm_calls"]
    store.create_request(legacy)
    with store._client.engine.begin() as conn:
        conn.execute(
            text("UPDATE requests SET envelope_json = :j WHERE request_id = :r"),
            {"j": json.dumps(raw), "r": legacy.request_id},
        )
    assert store.llm_calls_today("u1", now) == 2
    assert store.llm_calls_today("u1", now, exclude_request_id=env.request_id) == 0
