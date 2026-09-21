"""Env-var convention for LLM access, shared by all subprojects.

Precedence: explicit override kwarg > environment (incl. .env) > default.
Subprojects may have extra knobs (chunking, styles); those stay in *their*
config. This dataclass holds only provider-connection fields.
"""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv(encoding="utf-8-sig")

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_MODEL = "deepseek-chat"

#: env var -> LLMSettings field (the repo-wide contract)
ENV_MAP = {
    "LLM_API_KEY": "api_key",
    "LLM_BASE_URL": "base_url",
    "LLM_MODEL": "model",
    "LLM_TEMPERATURE": "temperature",
    "LLM_TIMEOUT": "timeout",
    "LLM_MAX_RETRIES": "max_retries",
}


@dataclass(frozen=True)
class LLMSettings:
    api_key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    temperature: float = 0.3
    timeout: int = 120
    max_retries: int = 3
    rate_limits: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_env(cls, **overrides) -> "LLMSettings":
        """Build settings from env/.env, letting non-None overrides win."""
        kwargs: dict = {}
        for env_key, attr in ENV_MAP.items():
            raw = os.getenv(env_key)
            if raw is None:
                continue
            ftype = cls.__annotations__[attr]  # type: ignore[misc]
            if ftype is int:
                kwargs[attr] = int(raw)
            elif ftype is float:
                kwargs[attr] = float(raw)
            else:
                kwargs[attr] = raw

        raw_limits = os.getenv("LLM_RATE_LIMITS")
        if raw_limits:
            kwargs["rate_limits"] = _parse_rate_limits(raw_limits)

        valid = set(cls.__annotations__)
        kwargs.update(
            {k: v for k, v in overrides.items() if v is not None and k in valid}
        )
        return cls(**kwargs)

    def require_api_key(self) -> None:
        """Call-time check: construction without a key is fine (offline tests),
        a real request without one is a misconfiguration worth naming."""
        if not self.api_key:
            raise RuntimeError(
                "LLM_API_KEY is not set. Add it to the repo-root .env "
                "(LLM_API_KEY=sk-...) or pass api_key= explicitly."
            )


def _parse_rate_limits(raw: str) -> dict[str, int]:
    """Parse ``model1:rpm1,model2:rpm2`` into a dict.

    Example: ``LLM_RATE_LIMITS=deepseek-chat:10,deepseek-embedding:30``
    """
    result: dict[str, int] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) != 2:
            continue
        model, rpm_str = parts
        try:
            result[model.strip()] = int(rpm_str.strip())
        except ValueError:
            continue
    return result
