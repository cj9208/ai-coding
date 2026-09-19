"""搜索后端策略接口。

v1 只实现 metadata 后端；full context / semantic 后端以后各写一个
SearchBackend 子类并在 __init__.py 注册即可，前端通过 mode 参数无感切换。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session


@dataclass
class SearchQuery:
    q: str = ""
    mode: str = "metadata"
    project_id: int | None = None
    team_id: int | None = None
    uploader_id: int | None = None
    extension: str | None = None
    #: 排序，与 services.files.SORT_OPTIONS 的取值一致
    sort: str = "created_at_desc"
    page: int = 1
    page_size: int = 50


@dataclass
class SearchHit:
    id: int
    filename: str
    title: str | None
    project_id: int
    project_name: str
    uploader_name: str
    team_name: str | None
    extension: str | None
    size: int
    created_at: datetime | None
    #: 命中来源字段的可读标签（如 ["标题", "标签"]）；纯筛选无关键词时为空
    matched: list[str] = field(default_factory=list)


@dataclass
class SearchResult:
    hits: list[SearchHit] = field(default_factory=list)
    total: int = 0
    page: int = 1
    page_size: int = 50
    mode: str = "metadata"
    #: 严格匹配 0 命中、已放宽为"任一关键词"时为 True（页面据此提示用户）
    relaxed: bool = False


class SearchBackend(ABC):
    #: 模式名，即 API/前端传入的 mode 值
    name: str = ""
    #: 该后端当前是否可用（未实现的模式注册为占位，available=False）
    available: bool = False
    description: str = ""

    @abstractmethod
    def search(self, db: Session, query: SearchQuery) -> SearchResult: ...


_BACKENDS: dict[str, SearchBackend] = {}


def register(backend: SearchBackend) -> None:
    _BACKENDS[backend.name] = backend


def get_backend(name: str) -> SearchBackend:
    backend = _BACKENDS.get(name)
    if backend is None:
        raise KeyError(name)
    return backend


def list_modes() -> list[dict]:
    return [
        {"name": b.name, "available": b.available, "description": b.description}
        for b in _BACKENDS.values()
    ]
