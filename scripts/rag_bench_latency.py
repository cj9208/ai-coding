"""Detailed query latency breakdown for RAG retrieval.

Isolates each stage of the retrieve() pipeline and measures its cost
independently at a given corpus size. The goal is to answer: where does
the time go?

Stages measured:
  1. shape   — LexicalShaper CJK tokenization
  2. fts_sql — FTS5 BM25 MATCH query (the SQL itself)
  3. fetch   — assemble: load child chunks by ID
  4. expand  — assemble: parent expansion (extra SQL round-trip)
  5. total   — end-to-end engine.retrieve() (no LLM)

Usage::

    uv run python scripts/rag_bench_latency.py
    uv run python scripts/rag_bench_latency.py --runs 20 --top-k 10
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from sqlalchemy import text

from rag.assemble import _fetch_by_ids, assemble
from rag.engine import build_paths, retrieve
from rag.fuse import rrf_fuse
from rag.shape import LexicalShaper
from rag.store import RagStore

_BENCH_DATA = Path("cache/rag_bench/data")

_SAMPLE_QUERIES = [
    "公司的年假审批流程是什么",
    "微服务架构的通信协议",
    "资产负债表由哪些部分组成",
    "服务等级协议的具体条款",
    "用户留存率的提升策略",
    "灰度发布的回滚策略",
    "绩效考核标准有哪些",
    "API网关的设计原则",
    "保密协议的期限规定",
    "监控告警的阈值配置",
    "薪酬结构包含哪些部分",
    "数据库分片的实现方案",
    "知识产权归属的约定",
    "竞品分析的方法论",
    "故障定位的排查步骤",
    "社保缴纳的比例标准",
    "缓存策略的选型依据",
    "违约赔偿的计算方式",
    "日活跃用户的定义",
    "数据备份的恢复流程",
    "培训学时的最低要求",
    "负载均衡的配置方法",
    "仲裁条款的适用范围",
    "需求池的管理流程",
    "容量评估的指标体系",
]


def _time_stage(fn, *args, **kwargs) -> tuple[Any, float]:
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - t0) * 1000


def _breakdown_one(
    store: RagStore,
    query: str,
    shaper: LexicalShaper,
    paths: list,
    k: int,
) -> dict[str, Any]:
    """Measure each stage of retrieve() for one query."""
    stages: dict[str, float] = {}

    shaped, t = _time_stage(shaper.shape, query)
    stages["shape_ms"] = round(t, 3)

    fts_ms = 0.0
    candidates_list = []
    for p in paths:
        ((candidates, t),) = [_time_stage(p.search, shaped, k * 2)]
        fts_ms += t
        candidates_list.append((p.name, candidates.candidates))
    stages["fts_sql_ms"] = round(fts_ms, 3)

    fused, t = _time_stage(rrf_fuse, candidates_list)
    stages["fuse_ms"] = round(t, 3)

    picked = [c for c in fused if "rrf" in c.ranks][:k]
    child_ids = [c.chunk_id for c in picked]
    children, t = _time_stage(_fetch_by_ids, store.client, child_ids)
    stages["fetch_children_ms"] = round(t, 3)

    parent_ids = [c.parent_chunk_id for c in children.values() if c.parent_chunk_id]
    if parent_ids:
        _, t = _time_stage(_fetch_by_ids, store.client, parent_ids)
        stages["expand_parents_ms"] = round(t, 3)
    else:
        stages["expand_parents_ms"] = 0.0

    total_pack, t = _time_stage(
        assemble,
        store.client,
        query,
        fused,
        k,
        path_meta=[{"path": "fts", "relaxed": False}],
    )
    stages["assemble_total_ms"] = round(t, 3)

    _, t = _time_stage(retrieve, store, query, k, shaped=shaped)
    stages["retrieve_total_ms"] = round(t, 3)

    stages["n_candidates"] = len(picked)
    stages["n_children"] = len(children)

    return stages


def run_latency_analysis(
    n_runs: int = 10,
    k: int = 5,
    queries: list[str] | None = None,
) -> dict[str, Any]:
    data_dir = _BENCH_DATA
    store = RagStore(data_dir / "kb.db")

    try:
        version = store.active_version()
        if version is None:
            print("no active version — run rag_bench_run.py first", file=sys.stderr)
            return {}

        paths = build_paths(store, version)
        if not paths:
            print("no live representations", file=sys.stderr)
            return {}

        shaper = LexicalShaper()
        queries = queries or _SAMPLE_QUERIES

        with store.client.session() as db:
            n_chunks = db.execute(text("SELECT COUNT(*) FROM chunks")).one()[0]
            n_fts = db.execute(text("SELECT COUNT(*) FROM chunks_fts")).one()[0]

        print(
            f"corpus: {n_chunks:,} chunks, {n_fts:,} FTS rows, " f"version={version}",
            file=sys.stderr,
        )
        print(
            f"running {n_runs} rounds x {len(queries)} queries...",
            file=sys.stderr,
        )

        all_runs: list[list[dict]] = []
        for run_i in range(n_runs):
            run_results = []
            for q in queries:
                bd = _breakdown_one(store, q, shaper, paths, k)
                bd["query"] = q[:40]
                run_results.append(bd)
            all_runs.append(run_results)
            if (run_i + 1) % 5 == 0:
                print(f"  run {run_i + 1}/{n_runs}", file=sys.stderr)

        stage_keys = [
            "shape_ms",
            "fts_sql_ms",
            "fuse_ms",
            "fetch_children_ms",
            "expand_parents_ms",
            "assemble_total_ms",
            "retrieve_total_ms",
        ]

        summary: dict[str, Any] = {
            "corpus": {
                "n_chunks": n_chunks,
                "n_fts_rows": n_fts,
                "version": version,
            },
            "config": {"n_runs": n_runs, "n_queries": len(queries), "k": k},
            "stages": {},
            "per_query": [],
        }

        for key in stage_keys:
            values = [r[key] for run in all_runs for r in run]
            s = sorted(values)
            n = len(s)
            summary["stages"][key] = {
                "p50": round(s[int(n * 0.5)], 3),
                "p95": round(s[min(int(n * 0.95), n - 1)], 3),
                "p99": round(s[min(int(n * 0.99), n - 1)], 3),
                "mean": round(statistics.mean(s), 3),
                "max": round(s[-1], 3),
            }

        for qi, q in enumerate(queries):
            q_runs = [run[qi] for run in all_runs]
            avg_retrieve = statistics.mean(r["retrieve_total_ms"] for r in q_runs)
            avg_fts = statistics.mean(r["fts_sql_ms"] for r in q_runs)
            summary["per_query"].append(
                {
                    "query": q[:60],
                    "avg_retrieve_ms": round(avg_retrieve, 2),
                    "avg_fts_ms": round(avg_fts, 2),
                    "fts_pct": (
                        round(avg_fts / avg_retrieve * 100, 1)
                        if avg_retrieve > 0
                        else 0
                    ),
                }
            )

        return summary

    finally:
        store.dispose()


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG query latency breakdown")
    ap.add_argument("--runs", type=int, default=10, help="measurement rounds")
    ap.add_argument("--top-k", type=int, default=5, help="top-k for retrieval")
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("cache/rag_bench/latency_breakdown.json"),
        help="output file",
    )
    args = ap.parse_args()

    result = run_latency_analysis(n_runs=args.runs, k=args.top_k)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nresults written to {args.out}", file=sys.stderr)

    if result.get("stages"):
        print(
            f"\n{'stage':<25} {'P50':>8} {'P95':>8} {'P99':>8} {'mean':>8}",
            file=sys.stderr,
        )
        print("-" * 65, file=sys.stderr)
        for key, stats in result["stages"].items():
            print(
                f"{key:<25} "
                f"{stats['p50']:>7.2f} "
                f"{stats['p95']:>7.2f} "
                f"{stats['p99']:>7.2f} "
                f"{stats['mean']:>7.2f}",
                file=sys.stderr,
            )


if __name__ == "__main__":
    main()
