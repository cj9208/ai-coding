"""Collector (02 §3): deterministic network behavior; LLM only for extraction.

Pipeline per sub-query: search -> top-k fetch -> store Capture (raw,
immutable) -> extract Findings (LLM, quote-audited) -> store. Every budget
unit (search call, fetch, extraction LLM call) is counted here; the
orchestrator commits the total in the same transaction as batch artifacts.

Failure policy: adapter error -> retry x2 -> SourceFailure recorded; a dead
URL never hard-fails the session (02 §3).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field

from ..contracts.models import (
    ExtractionResult,
    Finding,
    ResearchBrief,
    SourceFailure,
    SubQuery,
)
from ..contracts.prompts import render_prompt
from ..storage.store import SessionStore
from .adapters.base import AdapterError, SourceAdapter
from .normalizer import SupportClusterer, claim_tokens, quote_ok

logger = logging.getLogger(__name__)

TOP_K = 3
RETRIES = 2
MAX_EXTRACT_CHARS = 4000  # chars of a page handed to the extractor


@dataclass
class BatchOutcome:
    findings: list[Finding] = field(default_factory=list)
    failures: list[SourceFailure] = field(default_factory=list)
    calls_used: int = 0
    quotes_rejected: int = 0
    captures_new: int = 0
    captures_deduped: int = 0


class Collector:
    def __init__(self, llm, adapters: Mapping[str, SourceAdapter], store: SessionStore):
        self.llm = llm
        self.adapters = adapters
        self.store = store

    async def run_batch(
        self,
        brief: ResearchBrief,
        sub_queries: list[SubQuery],
        *,
        existing_findings: list[Finding],
        batch_no: int,
        calls_left: int,
    ) -> BatchOutcome:
        out = BatchOutcome()
        clusterer = SupportClusterer()
        clusterer.restore_from(
            {
                f.support_key: claim_tokens(f.claim)
                for f in existing_findings
                if f.support_key
            }
        )
        criteria = sorted({c for f in existing_findings for c in f.decision_criteria})
        for sq in sub_queries:
            if out.calls_used >= calls_left:
                out.failures.append(
                    SourceFailure(
                        query_id=sq.id,
                        adapter=sq.adapter_hint,
                        error="budget exhausted before this query ran",
                    )
                )
                continue
            await self._run_one(
                brief, sq, clusterer, criteria, batch_no, calls_left, out
            )
        return out

    async def _run_one(
        self,
        brief,
        sq: SubQuery,
        clusterer,
        criteria: list[str],
        batch_no: int,
        calls_left: int,
        out: BatchOutcome,
    ) -> None:
        adapter = self._pick(sq)
        try:
            hits = await self._with_retries(lambda: adapter.search(sq.query, TOP_K))
        except AdapterError as exc:
            out.calls_used += 1
            out.failures.append(
                SourceFailure(query_id=sq.id, adapter=adapter.kind, error=str(exc))
            )
            return
        out.calls_used += 1
        for hit in hits:
            if out.calls_used >= calls_left:
                out.failures.append(
                    SourceFailure(
                        query_id=sq.id,
                        adapter=adapter.kind,
                        error="batch cut short by budget",
                    )
                )
                return
            await self._capture_and_extract(
                brief, sq, hit, clusterer, criteria, batch_no, calls_left, out
            )

    async def _capture_and_extract(
        self,
        brief,
        sq: SubQuery,
        hit,
        clusterer,
        criteria: list[str],
        batch_no: int,
        calls_left: int,
        out: BatchOutcome,
    ) -> None:
        adapter = self._pick(sq)
        try:
            fetched = await self._with_retries(lambda: adapter.fetch(hit))
        except AdapterError as exc:
            out.calls_used += 1
            out.failures.append(
                SourceFailure(
                    query_id=sq.id, adapter=adapter.kind, url=hit.url, error=str(exc)
                )
            )
            return
        out.calls_used += 1
        capture_id, deduped = self.store.add_capture(
            brief.session_id,
            url=fetched.url,
            text=fetched.text,
            adapter=adapter.kind,
            title=fetched.title,
        )
        if deduped:
            # identical bytes were already extracted once — re-paying for the
            # same findings is waste, not thoroughness
            out.captures_deduped += 1
            return
        out.captures_new += 1
        if out.calls_used >= calls_left:
            out.failures.append(
                SourceFailure(
                    query_id=sq.id,
                    adapter="extract",
                    url=fetched.url,
                    error="extraction cut short by budget",
                )
            )
            return
        prompt = render_prompt(
            "extract",
            TOPIC=brief.topic,
            CRITERIA=", ".join(criteria) or "(none yet)",
            SOURCE_URL=fetched.url,
            CAPTURE_TEXT=fetched.text[:MAX_EXTRACT_CHARS],
        )
        try:
            draft: ExtractionResult = await self.llm.chat_json(
                prompt, schema=ExtractionResult, temperature=0.0
            )
        except Exception as exc:  # noqa: BLE001 — one bad page can't kill the batch
            out.calls_used += 1
            out.failures.append(
                SourceFailure(
                    query_id=sq.id, adapter="extract", url=fetched.url, error=str(exc)
                )
            )
            return
        out.calls_used += 1
        valid = [r for r in draft.findings if quote_ok(r.quote, fetched.text)]
        out.quotes_rejected += len(draft.findings) - len(valid)
        if out.quotes_rejected:
            logger.warning(
                "rejected %d findings with unmatched quotes (capture %s)",
                out.quotes_rejected,
                capture_id,
            )
        if not valid:
            return
        ids = self.store.allocate_finding_ids(brief.session_id, len(valid))
        findings = [
            Finding(
                id=fid,
                capture_id=capture_id,
                claim=r.claim,
                kind=r.kind,
                touches_candidates=r.touches_candidates,
                decision_criteria=r.decision_criteria,
                quote=r.quote,
                confidence=r.confidence,
                support_key=clusterer.key_for(r.claim),
                batch=batch_no,
            )
            for fid, r in zip(ids, valid)
        ]
        self.store.add_findings(brief.session_id, findings, batch=batch_no)
        out.findings.extend(findings)

    # -- helpers ---------------------------------------------------------------

    def _pick(self, sq: SubQuery) -> SourceAdapter:
        if sq.adapter_hint in self.adapters:
            return self.adapters[sq.adapter_hint]
        return self.adapters.get("web_search") or next(iter(self.adapters.values()))

    @staticmethod
    async def _with_retries(coro_factory):
        last: Exception = AdapterError("never attempted")
        for _ in range(1 + RETRIES):
            try:
                return await coro_factory()
            except AdapterError as exc:
                last = exc
            except Exception as exc:  # noqa: BLE001 — normalize to AdapterError
                last = AdapterError(str(exc))
        raise last
