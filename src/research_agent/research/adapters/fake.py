"""Fake adapter: canned pages from a fixtures directory (05 §5.2).

Lets the FULL research pipeline run offline — search returns the fixture
files, fetch "downloads" them — which is what makes CI-safe end-to-end tests
possible without mocking the LLM boundary of the collector itself.
"""

from __future__ import annotations

from pathlib import Path

from .base import AdapterError, Fetched, Hit


class FakeAdapter:
    kind = "web_search"

    def __init__(self, fixture_dir: Path | str, max_hits: int = 3):
        self.dir = Path(fixture_dir)
        self.max_hits = max_hits

    async def search(self, q: str, k: int) -> list[Hit]:
        files = sorted(self.dir.glob("*.md")) + sorted(self.dir.glob("*.html"))
        if not files:
            raise AdapterError(f"no fixtures in {self.dir}")
        return [
            Hit(
                url=f"fixture://{f.name}",
                title=f.stem,
                snippet=f" canned page {f.name}",
            )
            for f in files[: min(k, self.max_hits)]
        ]

    async def fetch(self, hit: Hit) -> Fetched:
        name = hit.url.removeprefix("fixture://")
        path = self.dir / name
        if not path.exists():
            raise AdapterError(f"missing fixture {path}")
        return Fetched(
            url=hit.url, text=path.read_text(encoding="utf-8"), title=path.stem
        )
