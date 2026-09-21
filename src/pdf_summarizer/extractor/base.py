from abc import ABC, abstractmethod

from ..models import Document


class ExtractorBackend(ABC):
    @abstractmethod
    async def extract(self, filepath: str) -> Document: ...


async def auto_extract(filepath: str, backends: list[ExtractorBackend]) -> Document:
    first_error = None
    for backend in backends:
        try:
            doc = await backend.extract(filepath)
            total_chars = sum(len(p.text) for p in doc.pages)
            if total_chars > 50:
                return doc
        except ImportError:
            continue
        except Exception as e:
            # Keep the *first* backend's diagnosis: backends are ordered by
            # preference (PyMuPDF first, OCR fallback last), so the primary
            # backend's verdict — "corrupted or invalid", "no extractable
            # text" — is the one a caller can act on. A fallback's error is
            # just a failed attempt (e.g. the OCR engine's RuntimeError on a
            # corrupted input), not a better description of the problem.
            if first_error is None:
                first_error = e
            continue
    if first_error:
        raise first_error
    raise ValueError(f"No extractable text found in {filepath}")
