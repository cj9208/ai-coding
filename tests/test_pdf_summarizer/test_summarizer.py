import os

import pytest

from pdf_summarizer.config import Config
from pdf_summarizer.models import SummaryStyle
from pdf_summarizer.summarizer import summarize


@pytest.fixture
def llm_config():
    return Config(
        api_key="test-key",
        chunk_size=2000,
        chunk_overlap=50,
        max_concurrency=2,
        timeout=10,
        max_retries=1,
    )


@pytest.mark.skipif(
    not os.getenv("LLM_API_KEY"),
    reason="LLM_API_KEY not set — skipping live API test",
)
@pytest.mark.asyncio
async def test_summarize_live(sample_pdf_path, llm_config):
    config = Config(
        api_key=os.getenv("LLM_API_KEY", ""),
        base_url=os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1"),
        model=os.getenv("LLM_MODEL", "deepseek-chat"),
        chunk_size=2000,
        chunk_overlap=50,
        max_concurrency=2,
    )
    result = await summarize(sample_pdf_path, config)
    assert result
    assert isinstance(result, str)
    assert len(result) > 20


@pytest.mark.asyncio
async def test_summarize_with_mock(sample_pdf_path, llm_config):
    mock_response = "This is a mock summary of the document content."

    import pdf_summarizer.summarizer as mod

    original_cls = mod.LLMClient

    class MockClient:
        def __init__(self, config):
            pass

        async def chat(self, prompt, system_prompt=""):
            return mock_response

    mod.LLMClient = MockClient

    try:
        result = await summarize(sample_pdf_path, llm_config)
        assert result == mock_response
    finally:
        mod.LLMClient = original_cls


@pytest.mark.asyncio
async def test_summarize_style_prompts(sample_pdf_path):
    config = Config(
        api_key="test-key",
        chunk_size=2000,
        summary_style=SummaryStyle.BULLETS,
        max_retries=1,
    )
    mock_response = "- Point 1\n- Point 2"

    import pdf_summarizer.summarizer as mod

    original_cls = mod.LLMClient

    class MockClient:
        def __init__(self, config):
            pass

        async def chat(self, prompt, system_prompt=""):
            return mock_response

    mod.LLMClient = MockClient

    try:
        result = await summarize(sample_pdf_path, config)
        assert "Point 1" in result
    finally:
        mod.LLMClient = original_cls


@pytest.mark.asyncio
async def test_summarize_empty_pdf(empty_pdf_path, llm_config):
    with pytest.raises(ValueError, match="extractable"):
        await summarize(empty_pdf_path, llm_config)


@pytest.mark.asyncio
async def test_summarize_file_not_found(llm_config):
    with pytest.raises(Exception):
        await summarize("/nonexistent/file.pdf", llm_config)
