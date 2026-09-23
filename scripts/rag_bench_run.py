"""RAG retrieval benchmark — progressive scale-up with quality measurement.

Generates a synthetic corpus at a target scale, then measures build
(ingest+publish) and query (retrieve without LLM) performance at
progressive corpus sizes.

Usage::

    uv run python scripts/rag_bench_run.py --tiers 1000 10000 50000
    uv run python scripts/rag_bench_run.py --tiers 1000 10000 --skip-gen
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from rag_bench_gen import generate_corpus
from sqlalchemy import text

from rag.engine import retrieve
from rag.evaluation import GoldenCase, evaluate, load_cases
from rag.fts_path import Fts5Path
from rag.pipeline import ingest, publish
from rag.shape import LexicalShaper
from rag.store import RagStore

_BENCH_DATA = Path("cache/rag_bench/data")
_BENCH_INBOX = Path("cache/rag_bench/inbox")
_DEFAULT_TIERS = [1_000, 10_000, 50_000]


def _db_size_mb(db_path: Path) -> float:
    if db_path.exists():
        return db_path.stat().st_size / (1024 * 1024)
    return 0.0


def _fts_row_count(store: RagStore) -> int:
    with store.client.session() as db:
        row = db.execute(text("SELECT COUNT(*) FROM chunks_fts")).one()
        return int(row[0])


def _chunk_count(store: RagStore) -> int:
    with store.client.session() as db:
        row = db.execute(text("SELECT COUNT(*) FROM chunks")).one()
        return int(row[0])


def _time_build(inbox: Path, data_dir: Path, tier_limit: int) -> dict[str, Any]:
    """Run ingest + publish for the next tier, return timing report."""
    store = RagStore(data_dir / "kb.db")
    try:
        t0 = time.monotonic()
        ingest_report = ingest(inbox, data_dir, store=store, limit=tier_limit)
        t_ingest = time.monotonic() - t0

        t0 = time.monotonic()
        publish_report = publish(data_dir, store=store)
        t_publish = time.monotonic() - t0

        return {
            "ingest_time_s": round(t_ingest, 3),
            "publish_time_s": round(t_publish, 3),
            "total_time_s": round(t_ingest + t_publish, 3),
            "docs_chunked": ingest_report["chunked_docs"],
            "docs_skipped": ingest_report["skipped_docs"],
            "corpus_version": publish_report["corpus_version"],
            "doc_count": publish_report["doc_count"],
            "chunk_count": publish_report["chunk_count"],
            "fts_rows": _fts_row_count(store),
            "db_size_mb": round(_db_size_mb(data_dir / "kb.db"), 1),
        }
    finally:
        store.dispose()


def _time_queries(
    store: RagStore,
    cases: list[GoldenCase],
    k: int = 5,
) -> list[dict[str, Any]]:
    """Run each case through retrieve() and return per-case timing."""
    results = []
    for case in cases:
        t0 = time.monotonic()
        pack = retrieve(store, case.question, k)
        elapsed = time.monotonic() - t0
        results.append(
            {
                "case_id": case.case_id,
                "latency_ms": round(elapsed * 1000, 2),
                "n_candidates": len(pack.chunks),
                "insufficient": pack.insufficient,
            }
        )
    return results


def _time_fts_search(
    store: RagStore,
    cases: list[GoldenCase],
    k: int = 5,
) -> list[dict[str, Any]]:
    """Run Fts5Path.search() for each case — includes AND→OR relaxation."""
    shaper = LexicalShaper()
    version = store.active_version()
    if version is None:
        return []

    fts_path = Fts5Path(store.client, version)
    results = []
    for case in cases:
        shaped = shaper.shape(case.question)
        t0 = time.monotonic()
        outcome = fts_path.search(shaped, k * 2)
        elapsed = time.monotonic() - t0
        results.append(
            {
                "case_id": case.case_id,
                "fts_ms": round(elapsed * 1000, 2),
                "n_hits": len(outcome.candidates),
            }
        )
    return results


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


def _run_quality_eval(
    store: RagStore, cases: list[GoldenCase], k: int = 5
) -> dict[str, Any]:
    """Run the golden-set evaluation, return quality metrics."""
    result = evaluate(store, cases, k=k)
    return {
        "hit_rate": result["hit_rate"],
        "recall": result["recall"],
        "precision": result["precision"],
        "n_cases": result["n_cases"],
        "n_unresolved": len(result["unresolved_case_ids"]),
    }


def run_benchmark(
    tiers: list[int],
    *,
    skip_gen: bool = False,
    n_pages: int = 10,
    golden_path: Path | None = None,
) -> dict[str, Any]:
    max_tier = max(tiers)
    data_dir = _BENCH_DATA
    inbox = _BENCH_INBOX

    if not skip_gen:
        print(f"generating corpus: {max_tier} docs...", file=sys.stderr)
        golden_path = generate_corpus(max_tier, inbox, n_pages=n_pages)
    elif golden_path is None:
        golden_path = inbox.parent / "golden.jsonl"

    cases = load_cases(golden_path)
    print(f"loaded {len(cases)} golden queries", file=sys.stderr)

    results: dict[str, Any] = {"tiers": []}

    for tier in sorted(tiers):
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(f"tier: {tier:,} docs", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)

        print("  build...", file=sys.stderr)
        build_stats = _time_build(inbox, data_dir, tier_limit=tier)
        print(
            f"  build: {build_stats['total_time_s']:.1f}s "
            f"(ingest={build_stats['ingest_time_s']:.1f}s "
            f"publish={build_stats['publish_time_s']:.1f}s) "
            f"chunks={build_stats['chunk_count']:,} "
            f"fts_rows={build_stats['fts_rows']:,} "
            f"db={build_stats['db_size_mb']:.0f}MB",
            file=sys.stderr,
        )

        store = RagStore(data_dir / "kb.db")
        try:
            print("  query latency...", file=sys.stderr)
            query_timings = _time_queries(store, cases)
            latencies = [r["latency_ms"] for r in query_timings]
            latency_stats = _latency_stats(latencies)
            print(
                f"  retrieve: P50={latency_stats['p50']:.1f}ms "
                f"P95={latency_stats['p95']:.1f}ms "
                f"P99={latency_stats['p99']:.1f}ms "
                f"mean={latency_stats['mean']:.1f}ms",
                file=sys.stderr,
            )

            print("  FTS SQL latency...", file=sys.stderr)
            fts_timings = _time_fts_search(store, cases)
            fts_latencies = [r["fts_ms"] for r in fts_timings]
            fts_stats = _latency_stats(fts_latencies)
            print(
                f"  FTS SQL:  P50={fts_stats['p50']:.1f}ms "
                f"P95={fts_stats['p95']:.1f}ms "
                f"P99={fts_stats['p99']:.1f}ms",
                file=sys.stderr,
            )

            print("  quality eval...", file=sys.stderr)
            quality = _run_quality_eval(store, cases)
            print(
                f"  quality: hit_rate={quality['hit_rate']} "
                f"recall={quality['recall']} "
                f"precision={quality['precision']}",
                file=sys.stderr,
            )
        finally:
            store.dispose()

        overhead_stats = _latency_stats(
            [q["latency_ms"] - f["fts_ms"] for q, f in zip(query_timings, fts_timings)]
        )

        tier_result = {
            "n_docs": tier,
            "build": build_stats,
            "query_latency_ms": {
                "overall": latency_stats,
                "fts_sql": fts_stats,
                "overhead": overhead_stats,
            },
            "quality": quality,
            "per_case_query": query_timings,
            "per_case_fts": fts_timings,
        }
        results["tiers"].append(tier_result)

    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="RAG retrieval benchmark")
    ap.add_argument(
        "--tiers",
        type=int,
        nargs="+",
        default=_DEFAULT_TIERS,
        help="corpus sizes to measure (default: 1000 10000 50000)",
    )
    ap.add_argument(
        "--skip-gen",
        action="store_true",
        help="skip corpus generation (use existing inbox)",
    )
    ap.add_argument("--pages", type=int, default=10, help="pages per document")
    ap.add_argument(
        "--golden",
        type=Path,
        default=None,
        help="path to golden queries JSONL",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=Path("cache/rag_bench/results.json"),
        help="output results file",
    )
    args = ap.parse_args()

    results = run_benchmark(
        args.tiers,
        skip_gen=args.skip_gen,
        n_pages=args.pages,
        golden_path=args.golden,
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"\nresults written to {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
