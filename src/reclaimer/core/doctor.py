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


def classify_execution_platform(
    *, is_windows: bool, machine: str, product_name: str | None
) -> dict[str, str]:
    """Describe future execution support without authorizing any action.

    The current milestone remains inventory-only on every platform. This keeps the published
    Windows 11 x64 support boundary visible before an execution release exists.
    """

    if not is_windows:
        return {
            "status": "UNSUPPORTED",
            "detail": (
                "Reclaimer inventory is developed for Windows; execution support is Windows 11 "
                "x64 only."
            ),
        }
    if machine.casefold() not in {"amd64", "x86_64", "x64"}:
        return {
            "status": "UNSUPPORTED",
            "detail": "Future execution support requires a Windows 11 x64 host.",
        }
    if product_name is None:
        return {
            "status": "UNKNOWN",
            "detail": (
                "Windows product edition could not be read; future execution support cannot "
                "be determined."
            ),
        }
    if product_name.casefold().startswith("windows 11"):
        return {
            "status": "SUPPORTED_BASELINE",
            "detail": "Windows 11 x64 is the documented baseline for a future execution release.",
        }
    if product_name.casefold().startswith("windows 10"):
        return {
            "status": "BEST_EFFORT_INVENTORY",
            "detail": (
                "Windows 10 is inventory-only best effort; future execution support is not "
                "available."
            ),
        }
    return {
        "status": "UNKNOWN",
        "detail": (
            "This Windows product is outside the documented future execution support baseline."
        ),
    }


def _windows_product_name() -> str | None:
    """Read the local Windows product name without launching a command."""

    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            product_name, _ = winreg.QueryValueEx(key, "ProductName")
    except OSError:
        return None
    return product_name if isinstance(product_name, str) and product_name else None


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

    execution_platform = classify_execution_platform(
        is_windows=os.name == "nt",
        machine=platform.machine(),
        product_name=_windows_product_name(),
    )
    return {
        "reclaimer_version": __version__,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "is_windows": os.name == "nt",
        "process_elevated": elevated,
        "execution_allowed": False,
        "future_execution_platform": execution_platform,
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
