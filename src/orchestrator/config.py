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
    #: 05c quota gate: per-user daily LLM-call allowance, day-grain. Sized
    #: by ``docs/orchestrator/05c-cost-gate.md``'s cost model: the worst
    #: legal path spends ~5 calls/request, so 200 ≈ 40 answered requests a
    #: day — generous for a pilot human, a real ceiling against a looping
    #: script. Per-tenant values arrive with 05b's identity step.
    LLM_CALLS_PER_DAY = 200


class Thresholds:
    """Validation gates fed to the tables (CH02_03). Recorded-for-calibration
    numbers start permissive; golden cases tighten them."""

    GROUNDING_COVERAGE_MIN = 0.5
    #: DP-10 conservative retrieval: broaden top-k by this factor
    CONSERVATIVE_TOPK_FACTOR = 1.5


class FastPath:
    """05c step 3 (B.2): skip the flash interpret call when the
    deterministic pass alone carries strong evidence — a single alias
    candidate at full specificity (05c §3; the eligibility test is exactly
    ``strong_evidence``'s deterministic half, evaluated in ``interpret``).
    Default off until the parity tests are trusted in production; the
    safety gate runs before any path, deterministic or not (DP-2)."""

    ENABLED = os.getenv("ORCHESTRATOR_FAST_PATH", "") == "1"
    #: the synthesized proposal's task type — the whole "small
    #: fast-path-eligible set" the plan asked for, in one constant
    TASK_TYPE = "faq_howto"


class Lifecycle:
    """Wait bounds DP-4 does not cover. Budgets account *machine* time; a
    human's answer latency is subtracted out (``wall_clock_paused_ms``), so
    the side effect is that an ``awaiting_clarification`` request is
    resumable forever — against a registry, alias table and corpus that may
    have moved on (``docs/orchestrator/04-scaling.md`` §5). Resume past this
    gap re-interprets from a clean machine budget instead of continuing a
    stale interpretation mid-flight."""

    CLARIFICATION_TTL_MS = 7 * 24 * 3600 * 1000  # 7 days
