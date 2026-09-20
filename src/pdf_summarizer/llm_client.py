"""Thin adapter: pdf_summarizer's Config → the repo-wide shared LLM client.

All provider wiring (AsyncOpenAI construction, retries, timeouts) now lives in
the ``llm_client`` package. This shim only exists so the summarizer keeps its
own config object (CLI-flag overrides etc.) without depending on env names.
"""

from llm_client import LLMClient as SharedLLMClient
from llm_client.settings import LLMSettings


class LLMClient:
    def __init__(self, config):
        self._shared = SharedLLMClient(
            LLMSettings(
                api_key=config.api_key,
                base_url=config.base_url,
                model=config.model,
                temperature=config.temperature,
                timeout=config.timeout,
                max_retries=config.max_retries,
            )
        )
        # kept for existing tests/introspection
        self._model = config.model
        self._temperature = config.temperature

    async def chat(self, prompt: str, system_prompt: str = "") -> str:
        return await self._shared.chat(prompt, system_prompt)
