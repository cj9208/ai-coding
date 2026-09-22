"""Orchestrator write-path benchmark — metrics 1 and 3 of 05a step 1.

Two modes:

1. concurrency sweep (``--workers``): N worker processes drive the golden
   case suite against ONE shared SQLite db through the real ``Orchestrator``
   runtime. Every LLM seam is faked (FakeFrontHalf + golden registry), so
   the bench measures the harness + SQLite write path with zero LLM calls.
   LLM latency (1-10 s/call) would serialize workers and hide lock
   contention; fakes are also free and reproducible.

2. ops-surface seeding (``--seed-requests``): raw-insert N request rows +
   ~N/20 handoff objects, then time ``list_requests`` and
   ``objects_of_kind("handoff")`` — the two unindexed cross-request reads
   flagged in docs/orchestrator/04-scaling.md section 6.

Usage::

    uv run python scripts/orch_bench_run.py --workers 1 4 8 16 --turns 400
    uv run python scripts/orch_bench_run.py --seed-requests 100000
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import tempfile
import time
import traceback
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from orchestrator.capabilities.fake import FakeFrontHalf
from orchestrator.contracts import ExecutionBudget, RequestEnvelope
from orchestrator.golden import load_cases, registry_for
from orchestrator.ids import new_id
from orchestrator.runtime import Orchestrator
from orchestrator.store import Store

_BENCH_DIR = Path("data/orchestrator_bench")
_DEFAULT_CASES = Path("config/orchestrator/golden_cases.jsonl")
_WRITE_METHODS = ("create_request", "update_request", "append_object", "emit")


class TxnCountingStore(Store):
    """Counts two different things, because 05a step 3 made them differ:

    - ``writes``: Store write-method calls (the work the loop asks for);
    - ``commits``: actual transaction commits — one per ``transact()`` pass
      or per standalone writer, which is what the write lock sees.
    """

    def __init__(self, db_path: str | Path) -> None:
        super().__init__(db_path)
        self.writes: dict[str, int] = {m: 0 for m in _WRITE_METHODS}
        self.commits = 0

    @contextmanager
    def transact(self) -> Iterator[Any]:
        if self._outer is None:
            self.commits += 1
        with super().transact():
            yield

    @contextmanager
    def _write(self) -> Iterator[Any]:
        if self._outer is None:
            self.commits += 1  # a standalone writer commits on its own
        with super()._write() as conn:
            yield conn

    def create_request(self, envelope: RequestEnvelope) -> None:
        self.writes["create_request"] += 1
        super().create_request(envelope)

    def update_request(self, envelope: RequestEnvelope, now_ms: int) -> None:
        self.writes["update_request"] += 1
        super().update_request(envelope, now_ms)

    def append_object(
        self,
        request_id: str,
        kind: str,
        payload: Any,
        now_ms: int,
        seq: int | None = None,
    ) -> str:
        self.writes["append_object"] += 1
        return super().append_object(request_id, kind, payload, now_ms, seq)

    def emit(self, request_id: str, event: str, now_ms: int, **payload: Any) -> None:
        self.writes["emit"] += 1
        super().emit(request_id, event, now_ms, **payload)


def _status_value(status: Any) -> str:
    return status.value if isinstance(status, Enum) else str(status)


def _sweep_worker(job: tuple[str, int, int, int, str]) -> dict[str, Any]:
    """One worker process: n_turns random golden cases through run_turn(+resume).

    Cross-process same-request resume is deliberately not probed here — that
    lost-update race is a targeted 05a step-3 test, not bulk-bench material.
    """
    db_path, worker_id, n_turns, seed, cases_path = job
    cases = load_cases(cases_path)
    store = TxnCountingStore(db_path)
    rng = random.Random(
        seed + worker_id
    )  # nosec B311 - workload sampling, not security
    latencies: list[float] = []
    statuses: dict[str, int] = {}
    errors: dict[str, int] = {}
    tracebacks: dict[str, str] = {}
    tx_totals = {m: 0 for m in _WRITE_METHODS}
    commit_total = 0
    attempted = 0
    locked = 0
    try:
        for _ in range(n_turns):
            case = cases[rng.randrange(len(cases))]
            orch = Orchestrator(store, registry_for(case), FakeFrontHalf(case.turns))
            budget = ExecutionBudget(**case.budget) if case.budget else None
            attempted += 1
            t0 = time.monotonic()
            try:
                res = orch.run_turn(
                    case.input, user_id=f"bench_w{worker_id}", budget=budget
                )
                for answer in case.resume:
                    if _status_value(res.status) != "awaiting_clarification":
                        break
                    res = orch.resume(res.request_id, answer)
                latencies.append((time.monotonic() - t0) * 1000)
                st = _status_value(res.status)
                statuses[st] = statuses.get(st, 0) + 1
                for m in _WRITE_METHODS:
                    tx_totals[m] += store.writes[m]
                    store.writes[m] = 0
                commit_total += store.commits
                store.commits = 0
            except OperationalError as exc:
                if "locked" in str(exc):
                    locked += 1
                else:
                    key = f"OperationalError: {exc}"
                    errors[key] = errors.get(key, 0) + 1
                    tracebacks.setdefault(key, traceback.format_exc())
            except Exception as exc:  # noqa: BLE001 - bench tallies, never dies
                key = type(exc).__name__
                errors[key] = errors.get(key, 0) + 1
                tracebacks.setdefault(key, traceback.format_exc())
    finally:
        store.close()
    return {
        "worker_id": worker_id,
        "attempted": attempted,
        "locked": locked,
        "errors": errors,
        "tracebacks": tracebacks,
        "statuses": statuses,
        "latencies_ms": latencies,
        "tx_totals": tx_totals,
        "commit_total": commit_total,
    }


def _latency_stats(latencies: list[float]) -> dict[str, float]:
    if not latencies:
        return {"p50": 0, "p95": 0, "p99": 0, "mean": 0, "max": 0}
    s = sorted(latencies)
    n = len(s)
    return {
        "p50": round(s[int(n * 0.5)], 2),
        "p95": round(s[min(int(n * 0.95), n - 1)], 2),
        "p99": round(s[min(int(n * 0.99), n - 1)], 2),
        "mean": round(statistics.mean(s), 2),
        "max": round(s[-1], 2),
    }


def _run_sweep(
    workers_list: list[int], turns: int, seed: int, cases_path: Path
) -> list[dict[str, Any]]:
    _BENCH_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    for n_workers in workers_list:
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(f"sweep point: {n_workers} workers x {turns} turns", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)
        tmpdir = Path(tempfile.mkdtemp(prefix=f"orch_bench_w{n_workers}_"))
        db_path = tmpdir / "bench.db"
        jobs = [
            (str(db_path), i, turns, seed, str(cases_path)) for i in range(n_workers)
        ]

        t0 = time.monotonic()
        with ProcessPoolExecutor(max_workers=n_workers) as pool:
            worker_results = list(pool.map(_sweep_worker, jobs))
        wall_s = time.monotonic() - t0

        attempted = sum(w["attempted"] for w in worker_results)
        locked = sum(w["locked"] for w in worker_results)
        errors: dict[str, int] = {}
        tracebacks: dict[str, str] = {}
        statuses: dict[str, int] = {}
        latencies: list[float] = []
        tx_totals = {m: 0 for m in _WRITE_METHODS}
        for w in worker_results:
            latencies.extend(w["latencies_ms"])
            for k, v in w["errors"].items():
                errors[k] = errors.get(k, 0) + v
            for k, v in w["tracebacks"].items():
                tracebacks.setdefault(k, v)
            for k, v in w["statuses"].items():
                statuses[k] = statuses.get(k, 0) + v
            for m in _WRITE_METHODS:
                tx_totals[m] += w["tx_totals"][m]

        completed = len(latencies)
        latency_stats = _latency_stats(latencies)
        commits = sum(w["commit_total"] for w in worker_results)
        writes_per_turn = (
            round(sum(tx_totals.values()) / completed, 1) if completed else 0
        )
        commits_per_turn = round(commits / completed, 1) if completed else 0
        point = {
            "workers": n_workers,
            "turns_per_worker": turns,
            "attempted": attempted,
            "completed": completed,
            "locked": locked,
            "locked_rate": round(locked / attempted, 4) if attempted else 0,
            "errors": errors,
            "tracebacks": tracebacks,
            "statuses": statuses,
            "latency_ms": latency_stats,
            "writes_per_turn": writes_per_turn,
            "commits_per_turn": commits_per_turn,
            "write_mix": tx_totals,
            "wall_s": round(wall_s, 1),
        }
        results.append(point)
        print(
            f"  attempted={attempted} completed={completed} "
            f"locked={locked} ({point['locked_rate'] * 100:.2f}%)",
            file=sys.stderr,
        )
        print(
            f"  latency P50={latency_stats['p50']:.1f}ms "
            f"P95={latency_stats['p95']:.1f}ms P99={latency_stats['p99']:.1f}ms "
            f"mean={latency_stats['mean']:.1f}ms",
            file=sys.stderr,
        )
        print(
            f"  writes/turn={writes_per_turn} commits/turn={commits_per_turn}"
            f" mix={tx_totals}",
            file=sys.stderr,
        )
        if errors:
            print(f"  ERRORS: {errors}", file=sys.stderr)
            for key, tb in tracebacks.items():
                print(f"  first traceback for {key!r}:\n{tb}", file=sys.stderr)

        for suffix in ("", "-wal", "-shm"):
            (tmpdir / f"bench.db{suffix}").unlink(missing_ok=True)
        tmpdir.rmdir()
    return results


def _seed_and_measure(n_requests: int) -> dict[str, Any]:
    _BENCH_DIR.mkdir(parents=True, exist_ok=True)
    tmpdir = Path(tempfile.mkdtemp(prefix="orch_bench_seed_"))
    db_path = tmpdir / "bench.db"
    store = Store(db_path)

    template = RequestEnvelope.new(text="bench seed", user_id="bench")
    base_env = json.loads(template.model_dump_json())
    now_ms = template.timestamp_start_ms
    handoff_payload = json.dumps(
        {"handoff_id": "ho_seed", "reason": "bench seed", "packet_markdown": "# seed"}
    )

    print(f"seeding {n_requests:,} requests...", file=sys.stderr)
    t0 = time.monotonic()
    chunk: list[dict[str, Any]] = []
    objects: list[dict[str, Any]] = []
    with store._client.engine.begin() as conn:  # schema exists; private client ok here
        for i in range(n_requests):
            rid = new_id("req")
            env = dict(base_env)
            env["request_id"] = rid
            env["user_id"] = f"bench_u{i % 50}"
            chunk.append(
                {
                    "rid": rid,
                    "sid": None,
                    "uid": env["user_id"],
                    "status": "completed",
                    "env": json.dumps(env),
                    "c": now_ms + i,
                    "u": now_ms + i,
                }
            )
            if i % 20 == 0:
                objects.append(
                    {
                        "oid": new_id("hand"),
                        "rid": rid,
                        "kind": "handoff",
                        "seq": 1,
                        "payload": handoff_payload,
                        "c": now_ms + i,
                    }
                )
            if len(chunk) == 5000:
                conn.execute(
                    text(
                        "INSERT INTO requests (request_id, session_id, user_id, status,"
                        " envelope_json, created_at_ms, updated_at_ms)"
                        " VALUES (:rid, :sid, :uid, :status, :env, :c, :u)"
                    ),
                    chunk,
                )
                chunk = []
            if len(objects) == 5000:
                conn.execute(
                    text(
                        "INSERT INTO runtime_objects (object_id, request_id, kind, seq,"
                        " payload_json, created_at_ms)"
                        " VALUES (:oid, :rid, :kind, :seq, :payload, :c)"
                    ),
                    objects,
                )
                objects = []
        if chunk:
            conn.execute(
                text(
                    "INSERT INTO requests (request_id, session_id, user_id, status,"
                    " envelope_json, created_at_ms, updated_at_ms)"
                    " VALUES (:rid, :sid, :uid, :status, :env, :c, :u)"
                ),
                chunk,
            )
        if objects:
            conn.execute(
                text(
                    "INSERT INTO runtime_objects (object_id, request_id, kind, seq,"
                    " payload_json, created_at_ms)"
                    " VALUES (:oid, :rid, :kind, :seq, :payload, :c)"
                ),
                objects,
            )
    seed_s = time.monotonic() - t0
    db_size_mb = round(db_path.stat().st_size / (1024 * 1024), 1)
    print(f"seeded in {seed_s:.1f}s db={db_size_mb}MB", file=sys.stderr)

    def _time_call(fn: Any, reps: int = 7) -> dict[str, float]:
        fn()  # warmup
        lat = []
        for _ in range(reps):
            t0 = time.monotonic()
            fn()
            lat.append((time.monotonic() - t0) * 1000)
        return _latency_stats(lat)

    list_stats = _time_call(lambda: store.list_requests(limit=20))
    handoff_stats = _time_call(lambda: store.objects_of_kind("handoff"))
    store.close()

    result = {
        "n_requests": n_requests,
        "db_size_mb": db_size_mb,
        "list_requests_ms": list_stats,
        "objects_of_kind_handoff_ms": handoff_stats,
    }
    print(f"  list_requests(20):        P50={list_stats['p50']:.2f}ms", file=sys.stderr)
    print(
        f"  objects_of_kind(handoff): P50={handoff_stats['p50']:.2f}ms "
        f"P95={handoff_stats['p95']:.2f}ms",
        file=sys.stderr,
    )

    for suffix in ("", "-wal", "-shm"):
        (tmpdir / f"bench.db{suffix}").unlink(missing_ok=True)
    tmpdir.rmdir()
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="orchestrator write-path benchmark")
    ap.add_argument(
        "--workers",
        type=int,
        nargs="+",
        default=None,
        help="worker counts for the concurrency sweep (default: 1 4 8 16)",
    )
    ap.add_argument("--turns", type=int, default=400, help="turns per worker")
    ap.add_argument(
        "--seed-requests",
        type=int,
        default=None,
        help="run ops-surface seeding mode with N requests instead of the sweep",
    )
    ap.add_argument("--cases", type=Path, default=_DEFAULT_CASES)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument(
        "--out",
        type=Path,
        default=_BENCH_DIR / "results.json",
        help="output results file",
    )
    args = ap.parse_args()

    results: dict[str, Any] = {}
    if args.seed_requests is not None:
        results["ops_surface"] = _seed_and_measure(args.seed_requests)
    else:
        workers = args.workers if args.workers is not None else [1, 4, 8, 16]
        results["sweep"] = _run_sweep(workers, args.turns, args.seed, args.cases)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nresults written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
