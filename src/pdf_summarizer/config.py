"""Project-local knobs + one nested ``llm: LLMSettings``.

LLM connection fields are *not* flattened out of ``LLMSettings`` anymore:
the old version mirrored six fields and copy-looped them in ``load_config``.
Now ``Config`` owns a single ``llm`` settings object (built via the shared
env convention, CLI overrides included) and exposes read-only properties so
call sites can keep saying ``config.model`` / ``config.api_key``.
"""

import os
from dataclasses import dataclass, field

from llm_client.settings import LLMSettings

from .models import SummaryStyle

#: LLM connection fields owned by llm_client.LLMSettings (the repo contract)
_LLM_FIELDS = ("api_key", "base_url", "model", "temperature", "timeout", "max_retries")


@dataclass
class Config:
    # Extractor
    extractor_backend: str = "auto"

    # LLM connection — defaults mirror the shared env convention
    llm: LLMSettings = field(default_factory=LLMSettings)

    # Chunking
    chunk_size: int = 3000
    chunk_overlap: int = 200

    # Summarization
    summary_style: SummaryStyle = SummaryStyle.CONCISE
    max_concurrency: int = 4

    # Read-only mirrors of the LLM settings, kept so the CLI/chunker/
    # summarizer don't have to know which object the fields live in.
    @property
    def api_key(self) -> str:
        return self.llm.api_key

    @property
    def base_url(self) -> str:
        return self.llm.base_url

    @property
    def model(self) -> str:
        return self.llm.model

    @property
    def temperature(self) -> float:
        return self.llm.temperature

    @property
    def timeout(self) -> int:
        return self.llm.timeout

    @property
    def max_retries(self) -> int:
        return self.llm.max_retries


# Local (non-LLM) knobs only; LLM_* env vars are handled by LLMSettings.from_env.
ENV_MAP = {
    "CHUNK_SIZE": "chunk_size",
    "CHUNK_OVERLAP": "chunk_overlap",
    "MAX_CONCURRENCY": "max_concurrency",
    "SUMMARY_STYLE": "summary_style",
}


def load_config(**overrides) -> Config:
    # 1. LLM connection: env/.env first, explicit (CLI) overrides win —
    #    precedence already implemented by LLMSettings.from_env
    llm = LLMSettings.from_env(
        **{k: v for k, v in overrides.items() if k in _LLM_FIELDS}
    )
    config = Config(llm=llm)

    # 2. local env knobs
    for env_key, attr in ENV_MAP.items():
        env_val = os.getenv(env_key)
        if env_val is not None:
            field_type = type(getattr(config, attr))
            if field_type is int:
                setattr(config, attr, int(env_val))
            elif field_type is float:
                setattr(config, attr, float(env_val))
            elif attr == "summary_style":
                setattr(config, attr, SummaryStyle(env_val))
            else:
                setattr(config, attr, env_val)

    # 3. explicit overrides for local knobs win (LLM ones already merged)
    for key, value in overrides.items():
        if value is None or key in _LLM_FIELDS:
            continue
        if key in Config.__dataclass_fields__:
            if key == "summary_style" and isinstance(value, str):
                value = SummaryStyle(value)
            setattr(config, key, value)

    return config
