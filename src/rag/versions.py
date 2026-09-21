"""Pipeline fingerprint: the hash that decides when chunk ids change.

Everything that affects chunk *boundaries or identity* goes in; everything
that is a rebuildable projection over unchanged chunks stays out. See
``docs/rag/05-incremental-design.md`` §"The pipeline fingerprint".

In: chunker rules, window limits, section attribution, the id format.
Out: enrich prompt (→ ``inferred.prompt_ver``), embedding model
(→ ``embeddings.model_id``), BM25 weights (→ ``representations`` string).
"""

from __future__ import annotations

import json
from typing import Any

from storage import sha256_hex

PIPELINE: dict[str, Any] = {
    "chunker": "structure_aware@v1",
    "child_char_limit": 800,
    "structure": "v1",
    "id_format": "v2",
}

PROMPT_VER = "enrich_v1"


def pipeline_fp() -> str:
    """Stable short hash of the boundary-affecting settings."""
    return sha256_hex(json.dumps(PIPELINE, sort_keys=True), length=8)
