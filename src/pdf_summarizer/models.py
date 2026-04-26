from dataclasses import dataclass
from enum import Enum


class SummaryStyle(Enum):
    CONCISE = "concise"
    DETAILED = "detailed"
    BULLETS = "bullets"
    EXECUTIVE = "executive"

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            lower = value.lower()
            for member in cls:
                if member.value == lower:
                    return member
        return super()._missing_(value)

    def __str__(self):
        return self.value


@dataclass
class Page:
    page_number: int
    text: str


@dataclass
class Document:
    pages: list[Page]
    filename: str

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    @property
    def total_text(self) -> str:
        return "\n\n".join(p.text for p in self.pages)


@dataclass
class Chunk:
    text: str
    token_count: int
    start_page: int
    end_page: int
