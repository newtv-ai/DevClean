"""Environment diagnostics with no vendor commands or elevation."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from reclaimer import __version__
from reclaimer.adapters.catalog import list_descriptors
from reclaimer.core.paths import data_dir, state_path
from reclaimer.core.state import StateStore
from reclaimer.platform.windows.security import is_process_elevated
from reclaimer.platform.windows.volumes import is_local_fixed_path


def collect_diagnostics() -> dict[str, Any]:
    database = state_path()
    elevated = is_process_elevated()
    integrity: str
    if elevated:
        integrity = "not_checked_elevated"
    elif database.exists():
        try:
            with StateStore(database) as store:
                integrity = "ok" if store.integrity_check() else "failed"
        except (OSError, RuntimeError, ValueError):
            integrity = "unavailable"
    else:
        integrity = "not_created"

    return {
        "reclaimer_version": __version__,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "is_windows": os.name == "nt",
        "process_elevated": elevated,
        "execution_allowed": False,
        "inventory_allowed": not elevated,
        "data_dir": str(data_dir()),
        "data_dir_is_absolute": data_dir().is_absolute(),
        "state_database": str(database),
        "state_location_valid": validate_local_state_path(database),
        "state_integrity": integrity,
        "registered_adapter_ids": [item.adapter_id for item in list_descriptors()],
        "safety_message": (
            "Main process is elevated; exit and restart from a normal terminal."
            if elevated
            else "Inventory-only milestone; no cleaning actions are available."
        ),
    }


def validate_local_state_path(path: Path) -> bool:
    """Return whether a state path is absolute and not a UNC path."""

    text = str(path)
    return (
        path.is_absolute()
        and not text.startswith((r"\\", "//"))
        and is_local_fixed_path(path.parent)
    )
