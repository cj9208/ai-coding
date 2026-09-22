"""Central tunables: budget defaults (DP-4), thresholds, paths.

One home for every first-version constant so calibration is a diff, not a
grep. The ``max_wall_clock_ms`` relaxation (8s -> 30s) is the only departure
from CH02_01's defaults; see DP-4.
"""

from __future__ import annotations

import os

from utils.paths import data_dir

#: default on-disk location for the orchestrator SQLite file
DB_PATH = data_dir("orchestrator") / "orchestrator.db"


class Models:
    """Front-half model roles (CH01 stage 2: cheap flash first, one
    escalation tier). Empty string = fall through to the ``llm_client``
    default (``LLM_MODEL``); the env vars exist so a deployment can point
    escalation at a stronger model without a code change."""

    FLASH = os.getenv("ORCHESTRATOR_FLASH_MODEL", "")
    ESCALATED = os.getenv("ORCHESTRATOR_STRONG_MODEL", "")


class Budget:
    """Execution-budget defaults (DP-4). Kept as a plain namespace so the
    envelope stores per-request copies and tests can override one field."""

    MAX_TOTAL_LOOPS = 6
    MAX_TOOL_CALLS = 6  # 4 -> 6: rag chains retrieve->rerank->answer
    MAX_REINTERPRETATIONS = 2
    MAX_EXECUTION_RETRIES = 2
    MAX_CLARIFICATION_TURNS = 2
    MAX_MODEL_ESCALATIONS = 1
    MAX_WALL_CLOCK_MS = 30_000  # 8000 -> 30000: lexical query + LLM answer


class Thresholds:
    """Validation gates fed to the tables (CH02_03). Recorded-for-calibration
    numbers start permissive; golden cases tighten them."""

    GROUNDING_COVERAGE_MIN = 0.5
    #: DP-10 conservative retrieval: broaden top-k by this factor
    CONSERVATIVE_TOPK_FACTOR = 1.5
