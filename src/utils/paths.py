"""Repo-wide path anchors.

Single source of truth for REPO_ROOT: the per-project copies of
``Path(__file__).resolve().parents[2]`` were five instances of the same
expression, which is exactly the "still true after swapping the business
code" knowledge that belongs in a shared helper. All default data/output
paths must anchor here (see AGENTS.md "Storage conventions"), never cwd.
"""

from pathlib import Path

#: repository root (this file lives at src/utils/paths.py)
REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir(name: str) -> Path:
    """Default data directory for a project/script: ``<repo>/data/<name>``.

    Env overrides stay in each project — this only provides the fallback.
    """
    return REPO_ROOT / "data" / name
