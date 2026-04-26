import logging

from openai import (
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    InternalServerError,
    RateLimitError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

RETRYABLE = (
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
)


class LLMClient:
    def __init__(self, config):
        self._client = AsyncOpenAI(
            api_key=config.api_key,
            base_url=config.base_url,
            timeout=config.timeout,
        )
        self._model = config.model
        self._temperature = config.temperature
        self._max_retries = config.max_retries

    async def chat(self, prompt: str, system_prompt: str = "") -> str:
        retry_decorator = retry(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential(multiplier=1, min=1, max=30),
            retry=retry_if_exception_type(RETRYABLE),
        )

        @retry_decorator
        async def _call():
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            response = await self._client.chat.completions.create(
                model=self._model,
                messages=messages,
                temperature=self._temperature,
            )
            return response.choices[0].message.content or ""

        return await _call()
