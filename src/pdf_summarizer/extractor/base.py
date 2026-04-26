from abc import ABC, abstractmethod

from ..models import Document


class ExtractorBackend(ABC):
    @abstractmethod
    async def extract(self, filepath: str) -> Document: ...


async def auto_extract(filepath: str, backends: list[ExtractorBackend]) -> Document:
    last_error = None
    for backend in backends:
        try:
            doc = await backend.extract(filepath)
            total_chars = sum(len(p.text) for p in doc.pages)
            if total_chars > 50:
                return doc
        except ImportError:
            continue
        except Exception as e:
            last_error = e
            continue
    if last_error:
        raise last_error
    raise ValueError(f"No extractable text found in {filepath}")
