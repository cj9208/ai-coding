from pdf_summarizer.config import Config, load_config
from pdf_summarizer.models import SummaryStyle


def test_config_defaults():
    c = Config()
    assert c.extractor_backend == "auto"
    assert c.base_url == "https://api.deepseek.com/v1"
    assert c.model == "deepseek-chat"
    assert c.chunk_size == 3000
    assert c.chunk_overlap == 200
    assert c.max_concurrency == 4
    assert c.timeout == 120
    assert c.max_retries == 3
    assert c.temperature == 0.3
    assert c.summary_style == SummaryStyle.CONCISE
    assert c.api_key == ""


def test_load_config_defaults():
    c = load_config()
    assert c.model == "deepseek-chat"


def test_load_config_overrides():
    c = load_config(
        model="custom-model",
        chunk_size=1000,
        summary_style="bullets",
    )
    assert c.model == "custom-model"
    assert c.chunk_size == 1000
    assert c.summary_style == SummaryStyle.BULLETS


def test_load_config_env_overrides(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("CHUNK_SIZE", "500")

    c = load_config()
    assert c.api_key == "env-key"
    assert c.model == "env-model"
    assert c.chunk_size == 500


def test_load_config_kwarg_wins_over_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "env-model")
    c = load_config(model="cli-model")
    assert c.model == "cli-model"


def test_summary_style_enum_values():
    assert SummaryStyle("concise") == SummaryStyle.CONCISE
    assert SummaryStyle("bullets") == SummaryStyle.BULLETS
    assert str(SummaryStyle.DETAILED) == "detailed"
