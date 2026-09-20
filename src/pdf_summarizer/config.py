import os
from dataclasses import dataclass

from dotenv import load_dotenv

from llm_client.settings import LLMSettings

from .models import SummaryStyle

load_dotenv(encoding="utf-8-sig")

# Single source of truth for LLM connection defaults + env names is
# llm_client.LLMSettings; this project's Config only adds local knobs.
_LLM_DEFAULTS = LLMSettings()
_LLM_FIELDS = ("api_key", "base_url", "model", "temperature", "timeout", "max_retries")


@dataclass
class Config:
    # Extractor
    extractor_backend: str = "auto"

    # LLM — defaults mirror llm_client.LLMSettings (DeepSeek-compatible provider)
    api_key: str = _LLM_DEFAULTS.api_key
    base_url: str = _LLM_DEFAULTS.base_url
    model: str = _LLM_DEFAULTS.model
    temperature: float = _LLM_DEFAULTS.temperature

    # Chunking
    chunk_size: int = 3000
    chunk_overlap: int = 200

    # Summarization
    summary_style: SummaryStyle = SummaryStyle.CONCISE
    max_concurrency: int = 4

    # Resilience
    timeout: int = _LLM_DEFAULTS.timeout
    max_retries: int = _LLM_DEFAULTS.max_retries


# Local (non-LLM) knobs only; LLM_* env vars are handled by LLMSettings.from_env.
ENV_MAP = {
    "CHUNK_SIZE": "chunk_size",
    "CHUNK_OVERLAP": "chunk_overlap",
    "MAX_CONCURRENCY": "max_concurrency",
    "SUMMARY_STYLE": "summary_style",
}


def load_config(**overrides) -> Config:
    config = Config()

    # 1. shared env convention (LLM_API_KEY / LLM_BASE_URL / LLM_MODEL /
    #    LLM_TEMPERATURE / LLM_TIMEOUT / LLM_MAX_RETRIES) via llm_client
    llm = LLMSettings.from_env()
    for attr in _LLM_FIELDS:
        setattr(config, attr, getattr(llm, attr))

    # 2. local env knobs on top
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

    # 3. explicit overrides (CLI flags) win
    for key, value in overrides.items():
        if value is not None and key in Config.__dataclass_fields__:
            if key == "summary_style" and isinstance(value, str):
                value = SummaryStyle(value)
            setattr(config, key, value)

    return config
