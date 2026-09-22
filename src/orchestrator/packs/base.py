"""The two shapes every locale pack fills — leaf module so ``zh``/``en``
can import them without a package-level cycle."""

from __future__ import annotations

from dataclasses import dataclass

from ..safety import SafetyRow


@dataclass(frozen=True)
class PromptPack:
    """Line templates for ``interpret.LlmFrontHalf._prompt`` — named
    placeholders only, so the assembly code stays locale-agnostic."""

    system: str
    raw_input: str
    answer: str
    normalized_query: str
    alias_hits: str
    none_label: str
    join: str
    escalation: str
    instructions: str


@dataclass(frozen=True)
class LocalePack:
    locale: str
    safety_table: tuple[SafetyRow, ...]
    aliases: dict[str, tuple[str, ...]]
    prompt: PromptPack
