"""05a step 5 acceptance: the harness runs *inside* a running event loop.

The parent §1 blocker was that both probabilistic stages called
``asyncio.run`` inside the turn, which raises under an ASGI host. These
tests pin the win from the host's seat: everything awaited here executes
on pytest-asyncio's live loop, so a re-introduced nested ``asyncio.run``
fails loudly. The sync ``run_turn`` shim keeps working for CLI/golden —
and is asserted to fail fast (not hang) when misused inside a loop.
"""

from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from orchestrator.capabilities.fake import FakeFrontHalf
from orchestrator.capabilities.lookup import StructuredLookupCapability
from orchestrator.capabilities.rag import RagQueryCapability
from orchestrator.contracts import CapabilityContext, RequestEnvelope
from orchestrator.interpret import LlmFrontHalf
from orchestrator.registry import load_static
from orchestrator.runtime import Orchestrator
from orchestrator.store import Store

STRONG = {
    "task_type": "faq_howto",
    "top_match_score": 0.9,
    "candidate_count": 1,
    "model": {"confidence": 0.9},
}
WEAK_NEEDS_CLARIFY = {
    "task_type": "faq_howto",
    "model": {"confidence": 0.2},
    "user_resolvable_ambiguity": True,
    "clarification_question": "您指的是哪张卡？",
}


def _orch(tmp_path: Path, turns: list[dict[str, Any]] | None = None) -> Orchestrator:
    return Orchestrator(
        Store(tmp_path / "embed.db"),
        load_static(),
        FakeFrontHalf(turns or [STRONG]),
    )


# -- the runtime embeds ---------------------------------------------------------
async def test_run_turn_async_completes_inside_a_running_loop(tmp_path: Path) -> None:
    result = await _orch(tmp_path).run_turn_async("春晖省钱卡怎么续费")
    assert result.status.value == "completed"
    assert "春晖省钱卡" in result.response


async def test_clarify_then_resume_async_walks_the_loop_in_a_host(
    tmp_path: Path,
) -> None:
    orch = _orch(tmp_path, [WEAK_NEEDS_CLARIFY, STRONG])
    first = await orch.run_turn_async("这张卡怎么续费")
    assert first.status.value == "awaiting_clarification"
    second = await orch.resume_async(first.request_id, "春晖省钱卡")
    assert second.status.value == "completed"


async def test_the_sync_shim_fails_fast_inside_a_loop_instead_of_nesting(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="await run_turn_async\\(\\) instead"):
        _orch(tmp_path).run_turn("春晖省钱卡怎么续费")


# -- both slow points are genuinely awaitable -----------------------------------
class _StubLLM:
    async def chat_json(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        schema: Any = None,
        **kw: Any,
    ) -> Any:
        return schema.model_validate({"task_type": "faq_howto", "confidence": 0.9})


async def test_llm_front_half_awaits_the_client_with_no_nested_loop() -> None:
    out = await LlmFrontHalf(_StubLLM()).interpret(
        RequestEnvelope.new(text="春晖卡怎么续费")
    )
    assert out.interpretation.model.confidence == 0.9


async def test_lookup_capability_awaits_its_thread_hop() -> None:
    from file_manager.search.base import SearchResult

    class _Client:
        @contextmanager
        def session(self) -> Iterator[Any]:
            yield object()

    class _Backend:
        def search(self, db: Any, query: Any) -> SearchResult:
            return SearchResult(hits=[], total=0)

    cap = StructuredLookupCapability(client=_Client(), backend=_Backend())
    ctx = CapabilityContext(
        request_id="r", session_id="s", normalized_query="发票", task_type="lookup"
    )
    result = await cap.run(ctx)  # to_thread inside a live loop must not raise
    assert result.code == "insufficient_evidence"


# -- capability handle lifetime (05a step 5E) ------------------------------------
def test_store_handle_survives_unchanged_file(tmp_path: Path) -> None:
    cap = RagQueryCapability(kb_path=tmp_path / "kb.db")
    first = cap._get_store()
    assert cap._get_store() is first  # cached, not rebuilt per call


def test_store_handle_reopens_when_the_kb_file_is_replaced(tmp_path: Path) -> None:
    from rag.store import RagStore

    path = tmp_path / "kb.db"
    cap = RagQueryCapability(kb_path=path)
    first = cap._get_store()
    # a `rag build` swaps the file: new bytes under the same name
    first.dispose()
    path.unlink()
    RagStore(path).dispose()
    second = cap._get_store()
    assert second is not first
    assert cap._get_store() is second  # and then caches again
