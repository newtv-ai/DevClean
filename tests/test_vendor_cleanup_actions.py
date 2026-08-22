from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import devclean.core.conda_maintenance as conda_maintenance
import devclean.core.go_maintenance as go_maintenance
import devclean.core.nuget_maintenance as nuget_maintenance
import devclean.core.pip_maintenance as pip_maintenance
import devclean.core.pnpm_maintenance as pnpm_maintenance
import devclean.core.uv_maintenance as uv_maintenance
from devclean.core.conda_maintenance import (
    CondaCleanResult,
    CondaPackageCacheEntry,
    CondaStorageInventory,
)
from devclean.core.go_maintenance import (
    GoCacheCleanResult,
    GoCacheEntry,
    GoCacheKind,
    GoMaintenanceLane,
    GoStorageInventory,
)
from devclean.core.nuget_maintenance import (
    NuGetClearResult,
    NuGetLocalEntry,
    NuGetLocalKind,
    NuGetMaintenanceLane,
    NuGetStorageInventory,
)
from devclean.core.pip_maintenance import (
    PipCacheEntry,
    PipCachePurgeResult,
    PipStorageInventory,
)
from devclean.core.pnpm_maintenance import PnpmPruneResult, PnpmStorageInventory, PnpmStoreEntry
from devclean.core.uv_maintenance import UvCacheEntry, UvStorageInventory
from devclean.core.vendor_cleanup_actions import (
    VendorCleanupCandidate,
    VendorCleanupKind,
    VendorCleanupRefusal,
    execute_vendor_cleanup,
    inventory_vendor_cleanup_candidates,
)

_MIB = 1024**2


def _empty_other_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(()),
    )
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(()),
    )
    monkeypatch.setattr(
        pnpm_maintenance,
        "inventory_pnpm_storage",
        lambda _environment=None: PnpmStorageInventory(()),
    )
    monkeypatch.setattr(
        go_maintenance,
        "inventory_go_storage",
        lambda _environment=None: GoStorageInventory(()),
    )
    monkeypatch.setattr(
        nuget_maintenance,
        "inventory_nuget_storage",
        lambda _environment=None: NuGetStorageInventory(()),
    )
    monkeypatch.setattr(
        conda_maintenance,
        "inventory_conda_storage",
        lambda _environment=None: CondaStorageInventory(()),
    )


def test_inventory_promotes_all_nonempty_deterministic_actions_without_size_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_other_providers(monkeypatch)
    pip_cache = tmp_path / "pip"
    uv_cache = tmp_path / "uv"
    pnpm_store = tmp_path / "pnpm"
    go_build = tmp_path / "go-build"
    go_module = tmp_path / "go-module"
    nuget_http = tmp_path / "nuget-http"
    nuget_global = tmp_path / "nuget-global"
    conda_cache = tmp_path / "conda"
    for path in (
        pip_cache,
        uv_cache,
        pnpm_store,
        go_build,
        go_module,
        nuget_http,
        nuget_global,
        conda_cache,
    ):
        path.mkdir()

    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(pip_cache, 5 * _MIB, True, False, True),)
        ),
    )
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(
            (UvCacheEntry(uv_cache, 6 * _MIB, True, False),)
        ),
    )
    monkeypatch.setattr(
        pnpm_maintenance,
        "inventory_pnpm_storage",
        lambda _environment=None: PnpmStorageInventory(
            (PnpmStoreEntry(pnpm_store, 7 * _MIB, True, False),)
        ),
    )
    monkeypatch.setattr(
        go_maintenance,
        "inventory_go_storage",
        lambda _environment=None: GoStorageInventory(
            (
                GoCacheEntry(
                    GoCacheKind.BUILD,
                    go_build,
                    8 * _MIB,
                    True,
                    GoMaintenanceLane.DETERMINISTIC_CANDIDATE,
                    False,
                    "build cache",
                ),
                GoCacheEntry(
                    GoCacheKind.MODULE,
                    go_module,
                    90 * _MIB,
                    True,
                    GoMaintenanceLane.USER_REVIEW,
                    False,
                    "module cache",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        nuget_maintenance,
        "inventory_nuget_storage",
        lambda _environment=None: NuGetStorageInventory(
            (
                NuGetLocalEntry(
                    NuGetLocalKind.HTTP_CACHE,
                    nuget_http,
                    9 * _MIB,
                    True,
                    NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE,
                    False,
                    "http cache",
                ),
                NuGetLocalEntry(
                    NuGetLocalKind.GLOBAL_PACKAGES,
                    nuget_global,
                    100 * _MIB,
                    True,
                    NuGetMaintenanceLane.USER_REVIEW,
                    False,
                    "global packages",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        conda_maintenance,
        "inventory_conda_storage",
        lambda _environment=None: CondaStorageInventory(
            (CondaPackageCacheEntry(conda_cache, 10 * _MIB, True, False, "tarballs/index"),)
        ),
    )

    inventory = inventory_vendor_cleanup_candidates({})

    assert {item.kind for item in inventory.candidates} == {
        VendorCleanupKind.PIP_CACHE_PURGE,
        VendorCleanupKind.UV_CACHE_PRUNE,
        VendorCleanupKind.PNPM_STORE_PRUNE,
        VendorCleanupKind.GO_BUILD_CACHE_CLEAN,
        VendorCleanupKind.NUGET_HTTP_CACHE_CLEAR,
        VendorCleanupKind.CONDA_TARBALL_INDEX_CLEAN,
    }
    assert all(item.path not in {go_module, nuget_global} for item in inventory.candidates)
    assert inventory.observed_bytes == (5 + 6 + 7 + 8 + 9 + 10) * _MIB
    assert inventory.warnings == ()


def test_inventory_isolates_optional_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_other_providers(monkeypatch)
    uv_cache = tmp_path / "uv"
    uv_cache.mkdir()

    def fail_pip(_environment: object = None) -> PipStorageInventory:
        raise RuntimeError("broken pip discovery")

    monkeypatch.setattr(pip_maintenance, "inventory_pip_storage", fail_pip)
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(
            (UvCacheEntry(uv_cache, 7 * _MIB, True, False),)
        ),
    )

    inventory = inventory_vendor_cleanup_candidates({})

    assert len(inventory.candidates) == 1
    assert inventory.candidates[0].kind is VendorCleanupKind.UV_CACHE_PRUNE
    assert inventory.warnings == ("pip inventory: broken pip discovery",)


def test_candidate_cannot_be_forged_or_modified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_other_providers(monkeypatch)
    cache = tmp_path / "pip"
    cache.mkdir()
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(cache, 7 * _MIB, True, False, True),)
        ),
    )
    candidate = inventory_vendor_cleanup_candidates({}).candidates[0]

    with pytest.raises(VendorCleanupRefusal, match="audited inventory"):
        VendorCleanupCandidate(
            candidate_id="forged",
            kind=VendorCleanupKind.PIP_CACHE_PURGE,
            path=cache,
            observed_bytes=7 * _MIB,
            label="pip",
            reason="forged",
        )

    with pytest.raises(VendorCleanupRefusal, match="altered"):
        replace(candidate, path=tmp_path / "other")


def test_execute_dispatches_pip_candidate_back_to_pip_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_other_providers(monkeypatch)
    cache = tmp_path / "pip"
    cache.mkdir()
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(cache, 700 * _MIB, True, True, False),)
        ),
    )
    candidate = inventory_vendor_cleanup_candidates({}).candidates[0]
    seen: list[tuple[Path, object]] = []

    def purge(path: Path, environment: object = None) -> PipCachePurgeResult:
        seen.append((path, environment))
        return PipCachePurgeResult(
            cache_path=path,
            before_bytes=700 * _MIB,
            after_bytes=100 * _MIB,
            command=("pip-test", "cache", "purge"),
            output="purged",
        )

    monkeypatch.setattr(pip_maintenance, "purge_pip_cache", purge)

    environment = {"PIP_CACHE_DIR": str(cache)}
    result = execute_vendor_cleanup(candidate, environment)

    assert seen == [(cache, environment)]
    assert result.kind is VendorCleanupKind.PIP_CACHE_PURGE
    assert result.reclaimed_bytes == 600 * _MIB
    assert result.command == ("pip-test", "cache", "purge")


def test_execute_partial_gc_failure_never_falls_back_to_raw_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_other_providers(monkeypatch)
    store = tmp_path / "pnpm"
    store.mkdir()
    payload = store / "referenced.bin"
    payload.write_bytes(b"keep")
    monkeypatch.setattr(
        pnpm_maintenance,
        "inventory_pnpm_storage",
        lambda _environment=None: PnpmStorageInventory(
            (PnpmStoreEntry(store, 800 * _MIB, True, True),)
        ),
    )
    candidate = inventory_vendor_cleanup_candidates({}).candidates[0]

    def fail_vendor(path: Path, environment: object = None) -> PnpmPruneResult:
        assert path == store
        raise RuntimeError("vendor refused")

    monkeypatch.setattr(pnpm_maintenance, "prune_pnpm_store", fail_vendor)

    with pytest.raises(RuntimeError, match="vendor refused"):
        execute_vendor_cleanup(candidate, {})
    assert payload.read_bytes() == b"keep"


def test_go_nuget_and_conda_dispatch_to_exact_provider_operations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _empty_other_providers(monkeypatch)
    go_cache = tmp_path / "go"
    nuget_cache = tmp_path / "nuget"
    conda_cache = tmp_path / "conda"
    for path in (go_cache, nuget_cache, conda_cache):
        path.mkdir()

    monkeypatch.setattr(
        go_maintenance,
        "inventory_go_storage",
        lambda _environment=None: GoStorageInventory(
            (
                GoCacheEntry(
                    GoCacheKind.BUILD,
                    go_cache,
                    100 * _MIB,
                    True,
                    GoMaintenanceLane.DETERMINISTIC_CANDIDATE,
                    False,
                    "build",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        nuget_maintenance,
        "inventory_nuget_storage",
        lambda _environment=None: NuGetStorageInventory(
            (
                NuGetLocalEntry(
                    NuGetLocalKind.HTTP_CACHE,
                    nuget_cache,
                    200 * _MIB,
                    True,
                    NuGetMaintenanceLane.DETERMINISTIC_CANDIDATE,
                    False,
                    "http",
                ),
            )
        ),
    )
    monkeypatch.setattr(
        conda_maintenance,
        "inventory_conda_storage",
        lambda _environment=None: CondaStorageInventory(
            (CondaPackageCacheEntry(conda_cache, 300 * _MIB, True, False, "tarballs"),)
        ),
    )
    candidates = inventory_vendor_cleanup_candidates({}).candidates
    by_kind = {candidate.kind: candidate for candidate in candidates}

    monkeypatch.setattr(
        go_maintenance,
        "clean_go_cache",
        lambda kind, path, _environment=None: GoCacheCleanResult(
            kind,
            path,
            100 * _MIB,
            1 * _MIB,
            ("go", "clean", "-cache"),
            "done",
        ),
    )
    monkeypatch.setattr(
        nuget_maintenance,
        "clear_nuget_local",
        lambda kind, path, _environment=None: NuGetClearResult(
            kind,
            path,
            200 * _MIB,
            2 * _MIB,
            "done",
        ),
    )
    monkeypatch.setattr(nuget_maintenance, "dotnet_executable", lambda _environment=None: "dotnet")
    monkeypatch.setattr(
        conda_maintenance,
        "clean_conda_package_cache",
        lambda path, _environment=None: CondaCleanResult(
            path,
            300 * _MIB,
            250 * _MIB,
            ("conda", "clean", "--tarballs", "--index-cache"),
            "done",
        ),
    )

    go_result = execute_vendor_cleanup(by_kind[VendorCleanupKind.GO_BUILD_CACHE_CLEAN], {})
    nuget_result = execute_vendor_cleanup(by_kind[VendorCleanupKind.NUGET_HTTP_CACHE_CLEAR], {})
    conda_result = execute_vendor_cleanup(
        by_kind[VendorCleanupKind.CONDA_TARBALL_INDEX_CLEAN], {}
    )

    assert go_result.reclaimed_bytes == 99 * _MIB
    assert nuget_result.reclaimed_bytes == 198 * _MIB
    assert conda_result.reclaimed_bytes == 50 * _MIB
