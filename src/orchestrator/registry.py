"""Capability registry (CH02_01 object 7 at runtime).

Two views, one ``Registry`` interface the runtime uses:
- ``load_default()`` — the production view: entries from
  ``config/orchestrator/capabilities.yaml`` (the M2 promotion), with
  implementations bound by name (rag_query, structured_lookup);
  ``orchestrate registry check`` validates the file offline;
- ``load_static()`` — the code fixture for M0 tests and the golden runner
  (echo + scripted), so regression never depends on a built rag corpus.

DP-5: selection is single-level (task_type → entry); ``domain_scope`` is
carried on the entry for ownership/attribution, never consulted for routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from utils.paths import REPO_ROOT

from .contracts import CapabilityCatalogEntry, OutputContract

if TYPE_CHECKING:
    from .capabilities.protocol import Capability

HUMAN_HANDOFF = "human_handoff"

CAPABILITIES_PATH = REPO_ROOT / "config" / "orchestrator" / "capabilities.yaml"


@dataclass
class Registry:
    entries: dict[str, CapabilityCatalogEntry] = field(default_factory=dict)
    impls: dict[str, "Capability"] = field(default_factory=dict)

    def entry(self, name: str) -> CapabilityCatalogEntry:
        try:
            return self.entries[name]
        except KeyError:
            raise ValueError(f"capability not registered: {name}") from None

    def impl(self, name: str) -> "Capability":
        try:
            return self.impls[name]
        except KeyError:
            raise ValueError(f"capability has no implementation: {name}") from None

    def has_impl(self, name: str) -> bool:
        return name in self.impls

    def select(self, task_type: str) -> str | None:
        """First entry whose task_types_supported names this task type
        (``"*"`` matches all). No domain step — DP-5."""
        for name, entry in self.entries.items():
            types = entry.task_types_supported
            if task_type in types or "*" in types:
                if self.has_impl(name):
                    return name
        return None

    def fallback_for(self, name: str) -> str | None:
        """First declared fallback that actually has an implementation —
        ``human_handoff`` is escalation *routing*, not a runnable capability."""
        entry = self.entries.get(name)
        if not entry:
            return None
        for f in entry.fallbacks:
            if f != name and f != HUMAN_HANDOFF and self.has_impl(f):
                return f
        return None


def load_static() -> Registry:
    """The M0 registry: one echo capability for the scripted loop plus the
    human_handoff catalog entry (packet building lives in capabilities.builtin,
    so it needs no impl here)."""
    echo = CapabilityCatalogEntry(
        name="echo",
        owner="platform",
        domain_scope="global",
        capability_version="0.1.0",
        rollout_status="active",
        purpose="M0 scripted proof that the loop executes and validates",
        task_types_supported=["*"],
        use_when=["any request in zero-LLM M0 testing"],
        avoid_when=["production use"],
        tool_schema_bundle=["echo.run"],
        output_contract=OutputContract(required_fields=["text"]),
        fallbacks=[HUMAN_HANDOFF],
    )
    handoff = CapabilityCatalogEntry(
        name=HUMAN_HANDOFF,
        owner="platform",
        domain_scope="global",
        capability_version="0.1.0",
        rollout_status="active",
        purpose="escalation family: persist a handoff packet + markdown export",
        task_types_supported=["human_handoff"],
        use_when=["budget exhausted", "unresolvable ambiguity", "policy stop"],
        avoid_when=["anything the loop can still decide on"],
        tool_schema_bundle=[],
        output_contract=OutputContract(required_fields=["handoff_id"]),
        fallbacks=[],
    )
    from .capabilities.fake import EchoCapability

    return Registry(
        entries={echo.name: echo, handoff.name: handoff},
        impls={echo.name: EchoCapability()},
    )


def load_yaml(path: Path | None = None) -> dict[str, CapabilityCatalogEntry]:
    """Parse and validate the registry file. Every field in
    ``CapabilityCatalogEntry.REQUIRED_FIELDS`` must be *present* in the raw
    entry — pydantic defaults would silently fill them, which is exactly the
    half-written-config failure the check exists to catch."""
    import yaml

    raw = yaml.safe_load((path or CAPABILITIES_PATH).read_text(encoding="utf-8"))
    entries: dict[str, CapabilityCatalogEntry] = {}
    for row in (raw or {}).get("capabilities", []):
        missing = [f for f in CapabilityCatalogEntry.REQUIRED_FIELDS if f not in row]
        if missing:
            name = row.get("name", "<unnamed>")
            raise ValueError(f"capability {name}: missing required fields {missing}")
        entry = CapabilityCatalogEntry.model_validate(row)
        entries[entry.name] = entry
    return entries


def load_default() -> Registry:
    """The production registry: YAML entries, implementations bound by name.
    The rag import is lazy — validating the file (``registry check``) must
    not pull in the rag package or require a built corpus."""
    entries = load_yaml()
    impls: dict[str, Capability] = {}
    if "rag_query" in entries:
        from .capabilities.rag import RagQueryCapability

        impls["rag_query"] = RagQueryCapability()
    if "structured_lookup" in entries:
        from .capabilities.lookup import StructuredLookupCapability

        impls["structured_lookup"] = StructuredLookupCapability()
    return Registry(entries=entries, impls=impls)
