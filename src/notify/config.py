"""Paths and channel switches for the ledger — the package's only path-assembly point.

Env convention lives here and nowhere else (design §4.7), read from the
repo-root ``.env`` like ``llm_client.settings`` does:

- ``NOTIFY_DATA_DIR``      ledger directory (default ``data/notify/``, via utils.paths)
- ``NOTIFY_CHANNELS``      comma list of enabled channels (default ``stdout``)
- ``NOTIFY_EXPECTATIONS``  rules file (default ``config/notify/expectations.yaml``)

Channel *credentials* (``TELEGRAM_*``) belong to their adapter module, not
here — config knows which channels are on, never what they need to work.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

from utils.paths import REPO_ROOT, data_dir

load_dotenv(encoding="utf-8-sig")

DEFAULT_CHANNELS = "stdout"


def notify_data_dir() -> Path:
    raw = os.getenv("NOTIFY_DATA_DIR")
    return Path(raw) if raw else data_dir("notify")


def db_path(base: Path | None = None) -> Path:
    """Full path of the ledger file, creating the directory if needed."""
    d = base if base is not None else notify_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / "events.db"


def expectations_path() -> Path:
    """Tracked rules file (§4.5). Missing is legal — see :func:`rules.load`."""
    raw = os.getenv("NOTIFY_EXPECTATIONS")
    if raw:
        return Path(raw)
    return REPO_ROOT / "config" / "notify" / "expectations.yaml"


def enabled_channels() -> list[str]:
    raw = os.getenv("NOTIFY_CHANNELS", DEFAULT_CHANNELS)
    return [c.strip() for c in raw.split(",") if c.strip()]
