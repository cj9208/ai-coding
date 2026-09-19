"""提取器注册表：按扩展名分发。新增类型只需写一个 Extractor 子类并在此注册。"""

from __future__ import annotations

from pathlib import Path

from .base import ExtractionResult, Extractor
from .eml import EmlExtractor
from .office import DocxExtractor, XlsxExtractor
from .pdf import PdfExtractor
from .text import TextExtractor

_REGISTRY: list[Extractor] = [
    EmlExtractor(),
    PdfExtractor(),
    TextExtractor(),
    DocxExtractor(),
    XlsxExtractor(),
]


def extract_for(path: Path) -> ExtractionResult:
    ext = path.suffix.lower().lstrip(".")
    for extractor in _REGISTRY:
        if ext in extractor.extensions:
            try:
                return extractor.extract(path)
            except Exception:  # noqa: BLE001 - 提取失败永不阻塞上传
                return ExtractionResult()
    return ExtractionResult()
