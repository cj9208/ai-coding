from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExtractionResult:
    """提取结果。title 现在入库展示；text 现在存库，供将来的全文/语义搜索复用。"""

    title: str | None = None
    text: str | None = None
    extra: dict = field(default_factory=dict)  # 类型特有字段，如邮件的 from/to/date


class Extractor(ABC):
    #: 处理的小写扩展名集合，如 {"eml"}
    extensions: frozenset[str] = frozenset()

    @abstractmethod
    def extract(self, path: Path) -> ExtractionResult: ...
