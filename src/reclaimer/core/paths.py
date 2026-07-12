"""Local application paths.

The state directory is intentionally local and must never be placed on a network share. Tests
can override it with ``RECLAIMER_DATA_DIR``.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Return Reclaimer's per-user local data directory without creating it."""

    override = os.environ.get("RECLAIMER_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        return Path(local_appdata) / "Reclaimer"

    return Path.home() / ".local" / "share" / "reclaimer"


def state_path() -> Path:
    """Return the default SQLite state path."""

    return data_dir() / "state" / "reclaimer.db"


def reports_dir() -> Path:
    """Return the default local report directory."""

    return data_dir() / "reports"

