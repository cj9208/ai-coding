from __future__ import annotations

from pathlib import Path

from .base import ExtractionResult, Extractor


class TextExtractor(Extractor):
    extensions = frozenset({"txt", "md", "csv", "json", "log"})

    def extract(self, path: Path) -> ExtractionResult:
        text = path.read_text(encoding="utf-8", errors="ignore")
        return ExtractionResult(text=text or None)
