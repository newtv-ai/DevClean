from __future__ import annotations

from pathlib import Path

import pytest

import devclean.core.nuget_maintenance as nuget_maintenance
from devclean.core.nuget_maintenance import (
    NuGetClearResult,
    NuGetLocalEntry,
    NuGetLocalKind,
    NuGetMaintenanceLane,
    NuGetStorageInventory,
)
from devclean.core.vendor_cleanup_actions import (
    VendorCleanupKind,
    execute_vendor_cleanup,
    inventory_vendor_cleanup_candidates,
)


def test_nuget_vendor_result_reports_the_exact_provider_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "http-cache"
    cache.mkdir()
    monkeypatch.setattr(
        nuget_maintenance,
        "inventory_nuget_storage",
        lambda _environment=None: NuGetStorageInventory(
            (
                NuGetLocalEntry(
                    NuGetLocalKind.HTTP_CACHE,
                    cache,
                    17,
                    True,
                    NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE,
                    False,
                    "http cache",
                ),
            )
        ),
    )
    candidate = inventory_vendor_cleanup_candidates(
        {},
        kinds=frozenset({VendorCleanupKind.NUGET_HTTP_CACHE_CLEAR}),
    ).candidates[0]
    actual_command = (
        r"C:\Program Files\dotnet\dotnet.exe",
        "nuget",
        "locals",
        "http-cache",
        "--clear",
        "--force-english-output",
    )

    monkeypatch.setattr(
        nuget_maintenance,
        "clear_nuget_local",
        lambda kind, path, _environment=None: NuGetClearResult(
            kind,
            path,
            17,
            0,
            "cleared",
            actual_command,
        ),
    )

    result = execute_vendor_cleanup(candidate, {})

    assert result.command == actual_command
    assert result.reclaimed_bytes == 17
