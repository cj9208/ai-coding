from llm_client.settings import DEFAULT_MODEL, LLMSettings


def test_defaults():
    s = LLMSettings()
    assert s.model == DEFAULT_MODEL
    assert s.base_url == "https://api.deepseek.com/v1"
    assert s.temperature == 0.3


def test_from_env_reads_and_coerces(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_TEMPERATURE", "0.7")
    monkeypatch.setenv("LLM_MAX_RETRIES", "5")
    s = LLMSettings.from_env()
    assert s.api_key == "env-key"
    assert s.model == "env-model"
    assert s.temperature == 0.7
    assert s.max_retries == 5


def test_overrides_beat_env_but_none_does_not(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "env-model")
    assert LLMSettings.from_env(model="explicit").model == "explicit"
    assert LLMSettings.from_env(model=None).model == "env-model"
    # unknown keys are ignored, not exploded
    assert LLMSettings.from_env(not_a_field="x").model == "env-model"


def test_require_api_key_raises_only_when_missing(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    try:
        LLMSettings.from_env().require_api_key()
        raise AssertionError("expected RuntimeError")
    except RuntimeError as exc:
        assert "LLM_API_KEY" in str(exc)
    LLMSettings(api_key="k").require_api_key()  # no raise
