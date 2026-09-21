"""Repo-wide shared LLM client — the single place that talks to an
OpenAI-compatible provider (DeepSeek by default).

Why this package exists: every subproject that needs an LLM otherwise
re-implements the same wiring — base_url/api_key/model from env, an
``AsyncOpenAI`` instance, tenacity retries, JSON-mode parsing. That wiring is
provider knowledge, not feature knowledge, so it lives here once. Subprojects
keep their own ``Config`` (CLI flags etc.) and convert into ``LLMSettings``.

Usage from a subproject::

    from llm_client import LLMClient, LLMSettings

    client = LLMClient(LLMSettings.from_env(model="deepseek-chat"))
    text = await client.chat(prompt, system_prompt=SP)
    data = await client.chat_json(prompt, system_prompt=SP)

Or share one process-wide client (AsyncOpenAI pools its HTTP connection)::

    from llm_client import get_client
    text = await get_client().chat(prompt)

Environment convention (read from `.env` automatically):
    LLM_API_KEY      required to actually call a provider
    LLM_BASE_URL     default: https://api.deepseek.com/v1
    LLM_MODEL        default: deepseek-chat
    LLM_TEMPERATURE  default: 0.3
    LLM_TIMEOUT      default: 120 (seconds)
    LLM_MAX_RETRIES  default: 3 attempts
"""

from .client import LLMClient, get_client
from .rate_limit import RateLimiter, TokenBucket
from .settings import LLMSettings

__all__ = ["LLMClient", "LLMSettings", "RateLimiter", "TokenBucket", "get_client"]
