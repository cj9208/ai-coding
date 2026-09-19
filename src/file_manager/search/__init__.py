from .base import (
    SearchBackend,
    SearchHit,
    SearchQuery,
    SearchResult,
    get_backend,
    list_modes,
    register,
)
from .metadata import MetadataSearchBackend

__all__ = [
    "SearchBackend",
    "SearchHit",
    "SearchQuery",
    "SearchResult",
    "MetadataSearchBackend",
    "get_backend",
    "list_modes",
    "register",
]

register(MetadataSearchBackend())


class _UnavailableBackend(SearchBackend):
    """未实现模式的占位注册：在模式列表里可见但不可用。"""

    def __init__(self, name: str, description: str) -> None:
        self.name = name
        self.description = description

    def search(self, db, query):  # noqa: ANN001, ANN201, ARG002
        raise NotImplementedError


register(_UnavailableBackend("fulltext", "全文检索（规划中：索引文档正文）"))
register(_UnavailableBackend("semantic", "语义搜索（规划中：向量检索）"))
