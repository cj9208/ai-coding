from pdf_summarizer.config import Config
from pdf_summarizer.llm_client import LLMClient


def test_llm_client_instantiation():
    config = Config(api_key="test-key", max_retries=1)
    client = LLMClient(config)
    assert client is not None
    assert client._model == "deepseek-chat"
    assert client._temperature == 0.3
