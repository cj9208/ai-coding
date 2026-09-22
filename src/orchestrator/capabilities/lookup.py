"""Adapter over file_manager's metadata search (M3 second capability).

The plug-in claim in code: this module is the *only* new file the runtime
sees — a registry YAML entry + this adapter, zero runtime changes. It is as
deliberately dumb as the rag adapter:
- it runs one ``MetadataSearchBackend.search`` over the *existing*
  file_manager SQLite (FTS5 + LIKE fallback, CJK folding all reused from the
  domain, nothing re-implemented here — decision 2);
- zero hits is reported as ``weak/insufficient_evidence`` and one more
  capability decides; it never decides its own retry or switch (CH02_02);
- unlike rag there are no generated claims to ground: the records *are* the
  data, so the entry carries no ``grounding_coverage_min`` rule — its
  ``answer_markdown``/``records`` required fields are the contract.

It is reached today as ``rag_query``'s declared fallback, which is what
makes ``switch_capability`` (exec e4 / validation v3) actually runnable.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from storage import SqliteClient
from utils.paths import data_dir

from ..contracts import CapabilityContext, CapabilityResult, ResultStatus

DEFAULT_LIMIT = 10


class StructuredLookupCapability:
    """Capability-protocol implementation: one run = one metadata search."""

    def __init__(
        self,
        *,
        db_path: Path | None = None,
        limit: int = DEFAULT_LIMIT,
        client: Any = None,
        backend: Any = None,
    ) -> None:
        self._db_path = db_path or data_dir("file_manager") / "file_manager.db"
        self._limit = limit
        self._client = client
        self._backend = backend

    def run(self, ctx: CapabilityContext) -> CapabilityResult:
        query = ctx.normalized_query.strip()
        if not query:
            return CapabilityResult(
                status=ResultStatus.weak,
                code="user_constraint_missing",
                output={
                    "clarification": "想查哪类文件？给个关键词（文件名/标题/标签）即可。"
                },
            )
        try:
            result = self._search(query)
        except Exception as exc:  # a missing/locked file db is structured
            return CapabilityResult(
                status=ResultStatus.failed,
                code="dependency_unavailable",
                output={"error": str(exc)[:200]},
            )
        return self._translate(query, result)

    # -- internals ---------------------------------------------------------
    def _get_client(self) -> Any:
        if self._client is None:
            self._client = SqliteClient(self._db_path)
        return self._client

    def _get_backend(self) -> Any:
        if self._backend is None:
            # lazy: importing file_manager pulls in its web stack; tests
            # and `registry check` must not need it
            from file_manager.search.metadata import MetadataSearchBackend

            self._backend = MetadataSearchBackend()
        return self._backend

    def _search(self, query: str) -> Any:
        from file_manager.search.base import SearchQuery

        sq = SearchQuery(q=query, page_size=self._limit)
        client = self._get_client()

        @contextmanager
        def session() -> Iterator[Any]:
            with client.session() as db:
                yield db

        with session() as db:
            return self._get_backend().search(db, sq)

    def _translate(self, query: str, result: Any) -> CapabilityResult:
        tool_steps = [f"lookup.metadata(q={query!r}, limit={self._limit})"]
        records = [_record(hit, i) for i, hit in enumerate(result.hits)]
        evidence = [f"file_meta:{r['id']}" for r in records]
        output: dict[str, Any] = {
            "records": records,
            "result_count": result.total,
            "answer_markdown": _markdown(query, result),
        }
        if result.relaxed:
            output["match_mode"] = "relaxed"

        if result.total == 0:
            return CapabilityResult(
                status=ResultStatus.weak,
                code="insufficient_evidence",
                output=output,
                tool_steps=tool_steps,
            )
        return CapabilityResult(
            status=ResultStatus.success,
            output=output,
            evidence_refs=evidence,
            confidence_signals={"record_count": float(result.total)},
            tool_steps=tool_steps,
        )


def _record(hit: Any, i: int) -> dict[str, Any]:
    return {
        "id": hit.id,
        "rank": i + 1,
        "filename": hit.filename,
        "title": hit.title,
        "project_name": hit.project_name,
        "uploader_name": hit.uploader_name,
        "extension": hit.extension,
        "size": hit.size,
        "created_at": hit.created_at.isoformat() if hit.created_at else None,
        "matched": list(hit.matched),
    }


def _markdown(query: str, result: Any) -> str:
    if result.total == 0:
        return f"文件库中没有匹配「{query}」的记录。"
    mode = "（已放宽为任一关键词匹配）" if result.relaxed else ""
    lines = [f"共找到 {result.total} 条匹配「{query}」的文件记录{mode}：", ""]
    for r in result.hits:
        title = f" — {r.title}" if r.title else ""
        matched = f"；命中：{'、'.join(r.matched)}" if r.matched else ""
        lines.append(
            f"- [{r.id}] {r.filename}{title}（项目：{r.project_name}，"
            f"上传人：{r.uploader_name}{matched}）"
        )
    return "\n".join(lines)
