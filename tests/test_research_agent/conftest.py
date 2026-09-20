from __future__ import annotations

from pathlib import Path

import pytest

from research_agent.persistence import SessionStore

FIXTURES = Path(__file__).parent / "fixtures"


class ScriptedLLM:
    """Offline stand-in for llm_client.chat_json: canned outputs keyed by the
    schema model's class name, consumed in order. Asserting on `calls` lets a
    test prove 'the LLM was called exactly when it had to be' — the budget
    story from the design docs, tested mechanically."""

    def __init__(self, responses: dict[str, list[dict]]):
        self._responses = {k: list(v) for k, v in responses.items()}
        self.calls: list[str] = []

    async def chat_json(
        self, prompt, system_prompt="", *, schema=None, model=None, temperature=None
    ):
        key = schema.__name__ if schema else "raw"
        self.calls.append(key)
        queue = self._responses.get(key)
        if not queue:
            raise AssertionError(f"no scripted {key} left (calls so far: {self.calls})")
        payload = queue.pop(0)
        return schema.model_validate(payload) if schema else payload


@pytest.fixture()
def store(tmp_path) -> SessionStore:
    return SessionStore.open(tmp_path / "agent.db")


@pytest.fixture()
def fixture_dir() -> Path:
    return FIXTURES / "laptops"
