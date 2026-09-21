"""Project configuration: paths only. LLM knobs come from the shared
``llm_client`` env convention (LLM_API_KEY/LLM_BASE_URL/LLM_MODEL)."""

from __future__ import annotations

import os
from pathlib import Path

# repo-root anchoring per AGENTS.md; single definition in utils.paths
from utils import paths

DATA_DIR = Path(os.getenv("RESEARCH_AGENT_DATA_DIR", paths.data_dir("research_agent")))
DB_PATH = DATA_DIR / "agent.db"
REPORTS_DIR = DATA_DIR / "reports"

DEFAULT_DB_URL = f"sqlite:///{DB_PATH.as_posix()}"
