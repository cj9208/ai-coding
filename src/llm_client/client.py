"""The shared async LLM client.

Design notes (the "why", since the "what" is short):
- One ``LLMClient`` per settings object; ``get_client()`` gives a process-wide
  default so the underlying ``AsyncOpenAI`` connection pool is reused.
- Retries wrap only transient provider failures (rate limit, timeout,
  connection, 5xx). A 400 (bad request) is a bug — surface it immediately.
- ``chat_json`` parses strictly and, on malformed output, makes ONE repair
  turn feeding the actual error back to the model, then fails hard. Silent
  best-effort parsing of prose is how pipelines ship garbage.
"""

import json
import logging
from typing import Any, Sequence

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from pydantic import BaseModel, ValidationError
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from .rate_limit import RateLimiter
from .settings import LLMSettings

logger = logging.getLogger(__name__)

RETRYABLE = (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError)

Message = dict[str, str]


class LLMClient:
    def __init__(
        self,
        settings: LLMSettings | None = None,
        *,
        rate_limits: dict[str, int] | None = None,
        **overrides: Any,
    ):
        self.settings = settings or LLMSettings.from_env(**overrides)
        self._client = AsyncOpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
        )
        self._limiter = RateLimiter(rate_limits)
        self._limiter_started = False

    # -- public API ---------------------------------------------------------

    async def chat(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        return await self._chat_messages(
            _to_messages(prompt, system_prompt), model=model, temperature=temperature
        )

    async def chat_json(
        self,
        prompt: str,
        system_prompt: str = "",
        *,
        schema: type[BaseModel] | None = None,
        model: str | None = None,
        temperature: float | None = None,
    ) -> dict | list | BaseModel:
        """Ask for a JSON object; optionally validate against a pydantic model.

        Returns the parsed dict/list, or an instance of ``schema`` when given.
        Raises ``ValueError`` after one failed repair turn.
        """
        system = system_prompt or ""
        system += (
            "\n\nRespond with a single valid JSON object only — no prose, "
            "no markdown fences."
        )
        messages = _to_messages(prompt, system)
        raw = await self._chat_messages(messages, model=model, temperature=temperature)
        try:
            return self._post_parse(_parse_json(raw), schema)
        except (ValueError, ValidationError) as exc:
            err_msg = str(exc)  # `exc` itself is unbound after this block
            logger.warning(
                "LLM returned invalid JSON (%s); attempting one repair", err_msg
            )

        repair = messages + [
            {"role": "assistant", "content": raw},
            {
                "role": "user",
                "content": f"Your reply was not usable: {err_msg}. "
                "Output ONLY the corrected JSON object.",
            },
        ]
        raw = await self._chat_messages(repair, model=model, temperature=temperature)
        return self._post_parse(_parse_json(raw), schema)

    # -- internals ----------------------------------------------------------

    def _post_parse(self, data: Any, schema: type[BaseModel] | None):
        if schema is None:
            return data
        return schema.model_validate(data)

    async def _chat_messages(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        temperature: float | None = None,
    ) -> str:
        self.settings.require_api_key()
        params: dict = {
            "model": model or self.settings.model,
            "messages": list(messages),
            "temperature": (
                self.settings.temperature if temperature is None else temperature
            ),
        }
        await self._ensure_limiter_started()
        await self._limiter.acquire(params["model"])
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.settings.max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(RETRYABLE),
            reraise=True,
        ):
            with attempt:
                response = await self._client.chat.completions.create(**params)
        return response.choices[0].message.content or ""

    async def _ensure_limiter_started(self) -> None:
        if not self._limiter_started and self._limiter.configured_models:
            await self._limiter.start()
            self._limiter_started = True

    async def close(self) -> None:
        """Stop the rate limiter and close the underlying HTTP client."""
        await self._limiter.stop()
        await self._client.close()


def _to_messages(prompt: str, system_prompt: str) -> list[Message]:
    messages: list[Message] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})
    return messages


def _parse_json(raw: str) -> Any:
    text = raw.strip()
    if text.startswith("```"):  # tolerate fences despite instructions
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"not valid JSON: {exc}") from exc


# -- process-wide default ----------------------------------------------------

_default_client: LLMClient | None = None


def get_client(**overrides: Any) -> LLMClient:
    """Cached default client; pass overrides only on first call in a process."""
    global _default_client
    if _default_client is None:
        _default_client = LLMClient(**overrides)
    return _default_client
