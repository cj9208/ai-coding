from .base import AdapterError, Fetched, Hit, SourceAdapter
from .fake import FakeAdapter
from .page_fetch import PageFetchAdapter
from .web_search import WebSearchAdapter


def default_adapters() -> dict[str, SourceAdapter]:
    """v1 adapters (02 §3): web_search owns search, page_fetch owns URLs.

    local_kb (ai_market_radar) and user_docs (file_manager) are v1+ slots:
    same protocol, add them here.
    """
    search = WebSearchAdapter()
    return {
        "web_search": search,
        "page_fetch": PageFetchAdapter(),
    }


__all__ = [
    "AdapterError",
    "Fetched",
    "Hit",
    "SourceAdapter",
    "FakeAdapter",
    "PageFetchAdapter",
    "WebSearchAdapter",
    "default_adapters",
]
