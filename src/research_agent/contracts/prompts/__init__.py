"""Versioned phase prompts (05 §3): one markdown file per prompt, loaded by name.

Prompts are data, not code: the version string is pinned onto every session so
a replay knows exactly which prompt text produced which artifacts.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_VERSION = "v1"

_DIR = Path(__file__).parent


@lru_cache(maxsize=None)
def read_prompt(name: str) -> str:
    path = _DIR / f"{name}.md"
    if not path.exists():
        raise FileNotFoundError(f"missing prompt: {path}")
    return path.read_text(encoding="utf-8").strip()


def render_prompt(name: str, **tokens: str) -> str:
    """Fill ``{{token}}`` placeholders; prompt files keep literal JSON examples."""
    text = read_prompt(name)
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    return text
