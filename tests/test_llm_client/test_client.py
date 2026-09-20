from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from llm_client import LLMClient
from llm_client.settings import LLMSettings


class Answer(BaseModel):
    topic: str
    count: int


def make_client() -> LLMClient:
    return LLMClient(LLMSettings(api_key="test-key"))


def fake_create(reply):
    """Stand-in for openai chat.completions.create returning `reply`."""

    async def _create(**kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
        )

    return _create


async def test_chat_passes_system_and_user_messages():
    client = make_client()
    seen = {}

    async def _create(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=" hello "))]
        )

    client._client.chat.completions.create = _create
    out = await client.chat("prompt text", system_prompt="be brief")
    assert out == " hello "
    assert [m["role"] for m in seen["messages"]] == ["system", "user"]
    assert seen["model"] == client.settings.model


async def test_chat_without_api_key_fails_at_call_time():
    client = LLMClient(LLMSettings())  # construction is fine offline
    with pytest.raises(RuntimeError, match="LLM_API_KEY"):
        await client.chat("hi")


async def test_chat_json_parses_and_validates_against_schema():
    client = make_client()
    client._client.chat.completions.create = fake_create('{"topic": "x", "count": 3}')
    result = await client.chat_json("q", schema=Answer)
    assert isinstance(result, Answer)
    assert result.count == 3


async def test_chat_json_repairs_once_then_succeeds():
    client = make_client()
    calls = []

    async def _create(**kwargs):
        calls.append(kwargs["messages"])
        reply = "oops not json" if len(calls) == 1 else '{"topic": "y", "count": 1}'
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))]
        )

    client._client.chat.completions.create = _create
    result = await client.chat_json("q", schema=Answer)
    assert result.topic == "y"
    assert len(calls) == 2
    # repair turn must contain the model's bad output + the error
    assert "oops not json" in [m["content"] for m in calls[1]]


async def test_chat_json_fails_hard_after_one_bad_repair():
    client = make_client()
    client._client.chat.completions.create = fake_create("still not json")
    with pytest.raises(ValueError, match="not valid JSON"):
        await client.chat_json("q")


async def test_chat_json_strips_code_fences():
    client = make_client()
    client._client.chat.completions.create = fake_create(
        '```json\n{"topic": "z", "count": 9}\n```'
    )
    assert (await client.chat_json("q", schema=Answer)).count == 9
