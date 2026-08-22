from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import devclean.core.pip_maintenance as pip_maintenance
import devclean.core.uv_maintenance as uv_maintenance
from devclean.core.pip_maintenance import (
    PipCacheEntry,
    PipCachePurgeResult,
    PipStorageInventory,
)
from devclean.core.uv_maintenance import UvCacheEntry, UvPruneResult, UvStorageInventory
from devclean.core.vendor_cleanup_actions import (
    VendorCleanupCandidate,
    VendorCleanupKind,
    VendorCleanupRefusal,
    execute_vendor_cleanup,
    inventory_vendor_cleanup_candidates,
)

_MIB = 1024**2


def test_inventory_promotes_only_recommended_existing_provider_entries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pip_large = tmp_path / "pip-large"
    pip_small = tmp_path / "pip-small"
    uv_large = tmp_path / "uv-large"
    for path in (pip_large, pip_small, uv_large):
        path.mkdir()

    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (
                PipCacheEntry(pip_large, 700 * _MIB, True, True, False),
                PipCacheEntry(pip_small, 100 * _MIB, True, False, True),
                PipCacheEntry(tmp_path / "missing-pip", 900 * _MIB, False, True, False),
            )
        ),
    )
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(
            (
                UvCacheEntry(uv_large, 800 * _MIB, True, True),
                UvCacheEntry(tmp_path / "missing-uv", 900 * _MIB, False, True),
            )
        ),
    )

    inventory = inventory_vendor_cleanup_candidates({})

    assert [(item.kind, item.path) for item in inventory.candidates] == [
        (VendorCleanupKind.UV_CACHE_PRUNE, uv_large),
        (VendorCleanupKind.PIP_CACHE_PURGE, pip_large),
    ]
    assert inventory.estimated_bytes == 1500 * _MIB
    assert inventory.warnings == ()


def test_inventory_isolates_optional_provider_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uv_cache = tmp_path / "uv"
    uv_cache.mkdir()

    def fail_pip(_environment: object = None) -> PipStorageInventory:
        raise RuntimeError("broken pip discovery")

    monkeypatch.setattr(pip_maintenance, "inventory_pip_storage", fail_pip)
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(
            (UvCacheEntry(uv_cache, 700 * _MIB, True, True),)
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
    cache = tmp_path / "pip"
    cache.mkdir()
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(cache, 700 * _MIB, True, True, False),)
        ),
    )
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(()),
    )
    candidate = inventory_vendor_cleanup_candidates({}).candidates[0]

    with pytest.raises(VendorCleanupRefusal, match="audited inventory"):
        VendorCleanupCandidate(
            candidate_id="forged",
            kind=VendorCleanupKind.PIP_CACHE_PURGE,
            path=cache,
            estimated_bytes=700 * _MIB,
            label="pip",
            reason="forged",
        )

    with pytest.raises(VendorCleanupRefusal, match="altered"):
        replace(candidate, path=tmp_path / "other")


def test_execute_dispatches_pip_candidate_back_to_pip_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "pip"
    cache.mkdir()
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(cache, 700 * _MIB, True, True, False),)
        ),
    )
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(()),
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


def test_execute_dispatches_uv_candidate_and_never_raw_deletes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "uv"
    cache.mkdir()
    payload = cache / "keep.bin"
    payload.write_bytes(b"keep")
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(()),
    )
    monkeypatch.setattr(
        uv_maintenance,
        "inventory_uv_storage",
        lambda _environment=None: UvStorageInventory(
            (UvCacheEntry(cache, 800 * _MIB, True, True),)
        ),
    )
    candidate = inventory_vendor_cleanup_candidates({}).candidates[0]

    def fail_vendor(path: Path, environment: object = None) -> UvPruneResult:
        assert path == cache
        raise RuntimeError("vendor refused")

    monkeypatch.setattr(uv_maintenance, "prune_uv_cache", fail_vendor)

    with pytest.raises(RuntimeError, match="vendor refused"):
        execute_vendor_cleanup(candidate, {})
    assert payload.read_bytes() == b"keep"
