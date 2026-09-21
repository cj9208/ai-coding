"""Run tracing: OTel-shaped spans recorded at the composition roots.

Why this exists and why it is this small: every run of ``rag build/query/eval``
should leave a process record (per-stage timing, decision facts, failures)
without a single tracing line inside a stage implementation. The five
protocol seams plus ``pipeline.build`` / ``engine.retrieve`` are the only
places variation flows through, so tracing is a decorator in the GoF sense —
``Traced*`` wrapper shells opened here, at the roots — and stage files stay
untouched, exactly like every other plug-in.

The span dict matches the OpenTelemetry trace data model
(trace_id / span_id / parent_span_id / name / start / end / status /
attributes) and attribute names follow the GenAI conventions where one fits
(``gen_ai.*``). That is the whole contract: today the exporter is one JSONL
file per run under ``data/rag/traces/``; a real OTel exporter or a
Phoenix/Langfuse importer can read the same records without any call-site
change. We take OTel's *model*, not its dependency.

Two guarantees, in order of importance:
- a business exception is recorded on its span and re-raised unchanged;
- a tracing failure (disk full, bad dir) never breaks the pipeline — it
  degrades to one warning line on stderr.
"""

from __future__ import annotations

import json
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contract import CanonicalDoc, Chunk
from .protocols import (
    Acquirer,
    CandidatePath,
    ChunkStrategy,
    EnrichProvider,
    QueryShaper,
    SearchOutcome,
    ShapedQuery,
)

#: one trace's current innermost span — nesting is implicit, call sites
#: never pass span handles around (same mechanism as OTel's context)
_CURRENT: ContextVar[Span | None] = ContextVar("rag_current_span", default=None)


@dataclass
class Span:
    """One unit of pipeline work; the record shape OTel defines for it."""

    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    start: float
    end: float | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)

    def set(self, **attributes: Any) -> "Span":
        self.attributes.update(attributes)
        return self

    @property
    def duration_ms(self) -> float | None:
        if self.end is None:
            return None
        return round((self.end - self.start) * 1000, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "start": self.start,
            "end": self.end,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
        }


#: pre-allocated no-op span yielded by disabled tracers — avoids per-call
#: allocation when tracing is off (the common case outside debug runs)
_NO_OP_SPAN = Span(trace_id="", span_id="0", parent_span_id=None, name="", start=0.0)


class Tracer:
    """Collects one run's spans and exports them as JSONL on ``close()``.

    A disabled tracer (``Tracer.disabled()``) keeps the exact same call
    shape but records nothing, so composition roots stay branch-free.
    """

    def __init__(self, traces_dir: Path | None = None, run: str = "run") -> None:
        self.traces_dir = traces_dir
        self.run = run
        self.trace_id = (
            f"t-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:4]}"
        )
        self._spans: list[Span] = []
        self._child_counts: dict[str, int] = {}

    @classmethod
    def disabled(cls) -> "Tracer":
        return cls(traces_dir=None)

    @property
    def enabled(self) -> bool:
        return self.traces_dir is not None

    def _next_span_id(self, parent: Span | None) -> str:
        key = parent.span_id if parent else ""
        n = self._child_counts.get(key, 0) + 1
        self._child_counts[key] = n
        return f"{key}.{n}" if key else str(n)

    @contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span]:
        if not self.enabled:
            yield _NO_OP_SPAN
            return
        parent = _CURRENT.get()
        sp = Span(
            trace_id=self.trace_id,
            span_id=self._next_span_id(parent),
            parent_span_id=parent.span_id if parent else None,
            name=name,
            start=time.time(),
            attributes=dict(attributes),
        )
        self._spans.append(sp)
        token = _CURRENT.set(sp)
        try:
            yield sp
        except BaseException as exc:
            sp.status = "error"
            sp.set(
                **{"error.type": type(exc).__name__, "error.message": str(exc)[:200]}
            )
            raise
        finally:
            _CURRENT.reset(token)
            sp.end = time.time()

    def close(self) -> Path | None:
        """Flush the run to one JSONL file; never raises (guarantee #2)."""
        if not self.enabled or not self._spans:
            return None
        assert self.traces_dir is not None
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = self.traces_dir / f"{stamp}_{self.run}_{self.trace_id}.jsonl"
        body = (
            "\n".join(json.dumps(s.to_dict(), ensure_ascii=False) for s in self._spans)
            + "\n"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(body, encoding="utf-8")
        except OSError as exc:
            print(f"warning: trace export failed ({path}): {exc}", file=sys.stderr)
            return None
        return path


#: the branch-free stand-in every composition root defaults to
NO_TRACER = Tracer.disabled()


# --- the five protocol seams, wrapped -------------------------------------
# Each shell records only *summary facts* about the contract objects flowing
# through (ids, counts, decisions) — never payloads: the chunk text already
# lives in the store, addressed by the ids we put in attributes.


class TracedAcquirer:
    """Wraps an :class:`~rag.protocols.Acquirer`; span ``rag.acquire``."""

    def __init__(self, inner: Acquirer, tracer: Tracer = NO_TRACER) -> None:
        self.inner = inner
        self.tracer = tracer
        self.name = f"traced({getattr(inner, 'name', '?')})"

    def fetch(self, bundle: Path) -> CanonicalDoc:
        with self.tracer.span("rag.acquire", bundle=bundle.name) as sp:
            doc = self.inner.fetch(bundle)
            sp.set(
                doc_id=doc.doc_id,
                page_count=doc.source.page_count,
                publish_decision=doc.trust.publish_decision.value,
                risk_flags=len(doc.trust.risk_flags),
            )
        return doc


class TracedChunker:
    """Wraps a :class:`~rag.protocols.ChunkStrategy`; span ``rag.chunk``."""

    def __init__(self, inner: ChunkStrategy, tracer: Tracer = NO_TRACER) -> None:
        self.inner = inner
        self.tracer = tracer

    def split(self, doc: CanonicalDoc, *, fp: str = "") -> list[Chunk]:
        with self.tracer.span("rag.chunk", doc_id=doc.doc_id) as sp:
            chunks = self.inner.split(doc, fp=fp)
            sp.set(
                n_chunks=len(chunks),
                n_parents=sum(1 for c in chunks if c.is_parent),
            )
        return chunks


class TracedShaper:
    """Wraps a :class:`~rag.protocols.QueryShaper`; span ``rag.shape``."""

    def __init__(self, inner: QueryShaper, tracer: Tracer = NO_TRACER) -> None:
        self.inner = inner
        self.tracer = tracer

    def shape(self, query: str) -> ShapedQuery:
        with self.tracer.span("rag.shape") as sp:
            shaped = self.inner.shape(query)
            sp.set(n_tokens=len(shaped.tokens))
        return shaped


class TracedPath:
    """Wraps a :class:`~rag.protocols.CandidatePath`; span
    ``rag.path.<name>.search`` — fusion sees the same ``name`` as before."""

    def __init__(self, inner: CandidatePath, tracer: Tracer = NO_TRACER) -> None:
        self.inner = inner
        self.tracer = tracer
        self.name = inner.name

    def search(self, shaped: ShapedQuery, k: int) -> SearchOutcome:
        with self.tracer.span(
            f"rag.path.{self.name}.search",
            **{"gen_ai.retrieval.top_k": k},
        ) as sp:
            outcome = self.inner.search(shaped, k)
            sp.set(
                hits=len(outcome.candidates),
                **{
                    "gen_ai.retrieval.returned": [
                        c.chunk_id for c in outcome.candidates[:3]
                    ]
                },
            )
            if "relaxed" in outcome.meta:
                sp.set(relaxed=outcome.meta["relaxed"])
        return outcome


class TracedEnricher:
    """Wraps an :class:`~rag.protocols.EnrichProvider`; span ``rag.enrich``.

    One span per chunk batch plus per-chunk children would explode the
    record for a 500-chunk build; the batch span carries the count and an
    error names the chunk that failed (enrich itself loops in one call).
    """

    def __init__(self, inner: EnrichProvider, tracer: Tracer = NO_TRACER) -> None:
        self.inner = inner
        self.tracer = tracer
        self.name = f"traced({getattr(inner, 'name', '?')})"

    async def enrich(self, chunks: list[Chunk]) -> None:
        with self.tracer.span("rag.enrich", n_chunks=len(chunks)):
            await self.inner.enrich(chunks)


# --- offline reader used by ``rag traces`` ----------------------------------


def load_trace(path: Path) -> list[Span]:
    """Reparse one exported JSONL file back into spans (sorted, tree-ready)."""
    spans = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        spans.append(
            Span(
                trace_id=d["trace_id"],
                span_id=d["span_id"],
                parent_span_id=d["parent_span_id"],
                name=d["name"],
                start=d["start"],
                end=d["end"],
                status=d["status"],
                attributes=d.get("attributes", {}),
            )
        )
    spans.sort(key=lambda s: [int(p) for p in s.span_id.split(".")])
    return spans


def render_tree(spans: list[Span]) -> str:
    """Flatten one trace's spans into an indented tree, OTL-style."""
    if not spans:
        return "(empty trace)"
    by_parent: dict[str | None, list[Span]] = {}
    for s in spans:
        by_parent.setdefault(s.parent_span_id, []).append(s)
    lines = [
        f"trace {spans[0].trace_id} — {len(spans)} span(s), "
        f"{round((spans[0].duration_ms or 0) * 10) / 10} ms"
    ]

    def walk(span: Span, depth: int) -> None:
        pad = "  " * depth
        info = f"{span.name}  {span.duration_ms} ms"
        if span.status != "ok":
            info += f"  [{span.attributes.get('error.type', 'error')}]"
        for key, val in span.attributes.items():
            if key.startswith("error.") or isinstance(val, (list, dict)):
                continue
            text = str(val)
            info += f"  {key}={text[:40]}"
        lines.append(pad + info)
        for child in by_parent.get(span.span_id, []):
            walk(child, depth + 1)

    for root in by_parent.get(None, []):
        walk(root, 0)
    return "\n".join(lines)
