"""Tracing: span tree shape, the never-break guarantee, wrapper records, CLI.

The load-bearing assertions are the two guarantees from the module docstring:
business exceptions pass through unchanged (recorded on the span), and a
broken exporter can never take a run down with it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from click.testing import CliRunner, Result

from rag import cli
from rag.contract import Candidate, Chunk
from rag.engine import retrieve
from rag.ingest import OcrBundleAcquirer
from rag.pipeline import build
from rag.protocols import SearchOutcome, ShapedQuery
from rag.tracing import (
    TracedAcquirer,
    TracedEnricher,
    TracedPath,
    Tracer,
)


def run(*args: str) -> Result:
    return CliRunner().invoke(cli.cli, list(args))


def _spans(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- core: tree shape -------------------------------------------------------


def test_spans_nest_into_a_tree(tmp_path):
    tracer = Tracer(tmp_path / "traces", run="test")
    with tracer.span("root") as r:
        r.set(a=1)
        with tracer.span("child"):
            with tracer.span("grandchild"):
                pass
    path = tracer.close()
    assert path is not None
    records = _spans(path)
    assert [s["span_id"] for s in records] == ["1", "1.1", "1.1.1"]
    assert [s["parent_span_id"] for s in records] == [None, "1", "1.1"]
    assert len({s["trace_id"] for s in records}) == 1
    root = records[0]
    assert root["attributes"] == {"a": 1}
    assert root["duration_ms"] >= 0
    assert {"start", "end", "status", "name"} <= set(root)


def test_error_is_recorded_then_reraised(tmp_path):
    tracer = Tracer(tmp_path / "traces", run="test")
    with pytest.raises(ValueError):
        with tracer.span("boom"):
            raise ValueError("bad bundle")
    path = tracer.close()
    assert path is not None
    (record,) = _spans(path)
    assert record["status"] == "error"
    assert record["attributes"]["error.type"] == "ValueError"
    assert "bad bundle" in record["attributes"]["error.message"]


# --- core: the never-break guarantee ----------------------------------------


def test_export_failure_degrades_to_warning(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file, not a directory", encoding="utf-8")
    tracer = Tracer(blocker / "traces", run="test")
    with tracer.span("anything"):
        pass
    assert tracer.close() is None  # no raise, no partial file
    assert "trace export failed" in capsys.readouterr().err


def test_disabled_tracer_is_inert(tmp_path):
    traces_dir = tmp_path / "traces"
    tracer = Tracer.disabled()
    with tracer.span("root") as r:
        r.set(x=1)
        with tracer.span("child"):
            pass
    assert tracer.close() is None
    assert not traces_dir.exists()


# --- wrappers at the seams ---------------------------------------------------


class _FakePath:
    name = "fake"

    def search(self, shaped: ShapedQuery, k: int) -> SearchOutcome:
        return SearchOutcome(
            candidates=[Candidate(chunk_id=f"c{i}") for i in range(k)],
            meta={"path": self.name, "relaxed": True},
        )


def test_traced_path_keeps_name_and_records_facts(tmp_path):
    tracer = Tracer(tmp_path / "traces", run="test")
    wrapped = TracedPath(_FakePath(), tracer)
    assert wrapped.name == "fake"  # fusion keys on it; the shell must not hide it
    outcome = wrapped.search(ShapedQuery(raw="q", tokens=["q"]), k=2)
    assert len(outcome.candidates) == 2
    path = tracer.close()
    assert path is not None
    (record,) = _spans(path)
    assert record["name"] == "rag.path.fake.search"
    attrs = record["attributes"]
    assert attrs["hits"] == 2
    assert attrs["relaxed"] is True
    assert attrs["gen_ai.retrieval.top_k"] == 2
    assert attrs["gen_ai.retrieval.returned"] == ["c0", "c1"]


def test_traced_acquirer_records_decision(tmp_path, inbox):
    tracer = Tracer(tmp_path / "traces", run="test")
    doc = TracedAcquirer(OcrBundleAcquirer(), tracer).fetch(inbox / "leave.ocr.json")
    path = tracer.close()
    assert path is not None
    (record,) = _spans(path)
    assert record["name"] == "rag.acquire"
    assert record["attributes"]["doc_id"] == doc.doc_id
    assert record["attributes"]["publish_decision"] == doc.trust.publish_decision.value


# --- end-to-end over the real pipeline ---------------------------------------


def test_build_trace_covers_every_bundle(tmp_path, inbox):
    tracer = Tracer(tmp_path / "traces", run="build")
    report = build(inbox, tmp_path / "data", tracer=tracer)
    assert not report["errors"]
    path = tracer.close()
    assert path is not None
    records = _spans(path)
    names = [s["name"] for s in records]
    assert names.count("rag.acquire") == 2  # the two fixture bundles
    assert names.count("rag.chunk") == 2
    root = next(s for s in records if s["parent_span_id"] is None)
    assert root["name"] == "rag.build"
    assert root["attributes"]["n_chunks"] == report["chunk_count"]
    assert root["attributes"]["corpus_version"] == report["corpus_version"]


def test_retrieve_trace_nests_shape_and_path_under_retrieve(built, tmp_path):
    store, _ = built
    tracer = Tracer(tmp_path / "traces", run="query")
    pack = retrieve(store, "年假审批", k=3, tracer=tracer)
    assert not pack.insufficient
    path = tracer.close()
    assert path is not None
    records = _spans(path)
    by_name = {s["name"]: s for s in records}
    retrieve_span = by_name["rag.retrieve"]
    assert by_name["rag.shape"]["parent_span_id"] == retrieve_span["span_id"]
    fts = by_name["rag.path.fts.search"]
    assert fts["parent_span_id"] == retrieve_span["span_id"]
    assert fts["attributes"]["hits"] > 0
    assert retrieve_span["attributes"]["n_candidates"] == pack.strength["n_candidates"]
    assert retrieve_span["attributes"]["insufficient"] is False


def test_enrich_span_is_async_safe(tmp_path):
    """TracedEnricher wraps an awaitable; the span must close around it."""

    class _Noop:
        name = "noop"

        async def enrich(self, chunks: list[Chunk]) -> None:
            await asyncio.sleep(0)

    tracer = Tracer(tmp_path / "traces", run="test")
    asyncio.run(TracedEnricher(_Noop(), tracer).enrich([]))  # type: ignore[arg-type]
    path = tracer.close()
    assert path is not None
    (record,) = _spans(path)
    assert record["name"] == "rag.enrich"
    assert record["status"] == "ok"


# --- CLI surface --------------------------------------------------------------


def test_cli_trace_flag_writes_and_lists(tmp_path, inbox):
    data = tmp_path / "cli-data"
    result = run("--data-dir", str(data), "build", "--inbox", str(inbox), "--trace")
    assert result.exit_code == 0, result.output
    assert "trace: " in result.stdout
    files = list((data / "traces").glob("*.jsonl"))
    assert len(files) == 1
    assert "_build_t-" in files[0].name  # <stamp>_build_<trace_id>.jsonl

    listing = run("--data-dir", str(data), "traces")
    assert listing.exit_code == 0, listing.output
    assert files[0].name in listing.stdout
    assert "rag.build" not in listing.stdout  # the list shows one summary line per file

    tree = run("--data-dir", str(data), "traces", files[0].name)
    assert tree.exit_code == 0, tree.output
    assert "rag.build" in tree.stdout
    assert "  rag.acquire" in tree.stdout  # children are indented under the root


def test_cli_query_trace_without_llm(tmp_path, inbox):
    data = tmp_path / "cli-data"
    assert run("--data-dir", str(data), "build", "--inbox", str(inbox)).exit_code == 0
    queried = run(
        "--data-dir",
        str(data),
        "query",
        "年假审批",
        "--retrieve-only",
        "--trace",
    )
    assert queried.exit_code == 0, queried.output
    assert "trace: " in queried.stdout
    (file,) = list((data / "traces").glob("*.jsonl"))
    names = {s["name"] for s in _spans(file)}
    assert {"rag.query", "rag.retrieve", "rag.shape", "rag.path.fts.search"} <= names


def test_cli_without_trace_touches_nothing(tmp_path, inbox):
    data = tmp_path / "cli-data"
    result = run("--data-dir", str(data), "build", "--inbox", str(inbox))
    assert result.exit_code == 0, result.output
    assert not (data / "traces").exists()


def test_traces_command_on_empty_dir(tmp_path):
    result = run("--data-dir", str(tmp_path / "d"), "traces")
    assert result.exit_code == 0
    assert "no traces yet" in result.stdout
