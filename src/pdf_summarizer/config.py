import os
from dataclasses import dataclass

from dotenv import load_dotenv

from .models import SummaryStyle

load_dotenv(encoding="utf-8-sig")


@dataclass
class Config:
    # Extractor
    extractor_backend: str = "auto"

    # LLM
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    temperature: float = 0.3

    # Chunking
    chunk_size: int = 3000
    chunk_overlap: int = 200

    # Summarization
    summary_style: SummaryStyle = SummaryStyle.CONCISE
    max_concurrency: int = 4

    # Resilience
    timeout: int = 120
    max_retries: int = 3


ENV_MAP = {
    "LLM_API_KEY": "api_key",
    "LLM_BASE_URL": "base_url",
    "LLM_MODEL": "model",
    "LLM_TEMPERATURE": "temperature",
    "CHUNK_SIZE": "chunk_size",
    "CHUNK_OVERLAP": "chunk_overlap",
    "MAX_CONCURRENCY": "max_concurrency",
    "LLM_TIMEOUT": "timeout",
    "LLM_MAX_RETRIES": "max_retries",
    "SUMMARY_STYLE": "summary_style",
}


def load_config(**overrides) -> Config:
    config = Config()

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

    for key, value in overrides.items():
        if value is not None and key in Config.__dataclass_fields__:
            if key == "summary_style" and isinstance(value, str):
                value = SummaryStyle(value)
            setattr(config, key, value)

    return config
