"""Environment diagnostics with no vendor commands or elevation."""

from __future__ import annotations

import os
import platform
import sys
from pathlib import Path
from typing import Any

from devclean import __version__
from devclean.adapters.catalog import list_descriptors
from devclean.core.paths import data_dir, state_path
from devclean.core.state import StateStore
from devclean.platform.windows.security import is_process_elevated
from devclean.platform.windows.volumes import is_local_fixed_path


def classify_execution_platform(
    *, is_windows: bool, machine: str, product_name: str | None, build_number: int | None
) -> dict[str, str]:
    """Describe the supported host baseline without authorizing any action.

    Confirmed cleanup exists in the GUI, and only on the documented baseline. This
    classification makes that boundary visible; it never grants an action.
    """

    if not is_windows:
        return {
            "status": "UNSUPPORTED",
            "detail": (
                "DevClean inventory is developed for Windows; execution support is Windows 11 "
                "x64 only."
            ),
        }
    if machine.casefold() not in {"amd64", "x86_64", "x64"}:
        return {
            "status": "UNSUPPORTED",
            "detail": "Confirmed cleanup requires a Windows 11 x64 host.",
        }
    product = product_name.casefold() if product_name is not None else ""
    if "server" in product:
        return {
            "status": "UNSUPPORTED",
            "detail": "Confirmed cleanup is limited to Windows 11 x64 client hosts.",
        }
    # The ProductName registry value can retain a Windows 10 string on Windows 11 upgrades.
    # Windows client build 22000 introduced Windows 11, so prefer the build when it is available.
    if product.startswith("windows 11") or (build_number is not None and build_number >= 22000):
        return {
            "status": "SUPPORTED_BASELINE",
            "detail": "Windows 11 x64 is the documented baseline for safe deletion support.",
        }
    if product_name is None:
        return {
            "status": "UNKNOWN",
            "detail": (
                "Windows product edition could not be read; the cleanup support "
                "baseline cannot be determined."
            ),
        }
    if product.startswith("windows 10"):
        return {
            "status": "BEST_EFFORT_INVENTORY",
            "detail": (
                "Windows 10 is inventory best effort; confirmed cleanup is not "
                "supported there."
            ),
        }
    return {
        "status": "UNKNOWN",
        "detail": (
            "This Windows product is outside the documented cleanup support baseline."
        ),
    }


def _windows_identity() -> tuple[str | None, int | None]:
    """Read local Windows release identifiers without launching a command."""

    if os.name != "nt":
        return (None, None)
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            product_name, _ = winreg.QueryValueEx(key, "ProductName")
            current_build, _ = winreg.QueryValueEx(key, "CurrentBuildNumber")
    except OSError:
        return (None, None)
    safe_product = product_name if isinstance(product_name, str) and product_name else None
    try:
        safe_build = int(current_build) if isinstance(current_build, str | int) else None
    except ValueError:
        safe_build = None
    return (safe_product, safe_build)


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

    product_name, build_number = _windows_identity()
    execution_platform = classify_execution_platform(
        is_windows=os.name == "nt",
        machine=platform.machine(),
        product_name=product_name,
        build_number=build_number,
    )
    return {
        "DevClean_version": __version__,
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "is_windows": os.name == "nt",
        "process_elevated": elevated,
        # Scoped to this process: the CLI has no cleanup surface at all.  It is
        # not a claim that the product cannot delete -- the GUI can, behind a
        # typed confirmation -- and ``doctor`` is where a user goes to learn the
        # safety boundary, so it must not deny a capability that exists.
        "execution_allowed": False,
        "confirmed_cleanup_surface": "GUI_ONLY",
        "execution_platform_baseline": execution_platform,
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
            else (
                "This CLI is read-only and has no cleanup command. Confirmed "
                "cleanup -- recoverable private quarantine, and irreversible "
                "purge only after a typed confirmation -- exists in the GUI."
            )
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
