from __future__ import annotations

from pathlib import Path

import pytest

import devclean.core.conda_maintenance as conda_maintenance
import devclean.core.go_maintenance as go_maintenance
import devclean.core.nuget_maintenance as nuget_maintenance
import devclean.core.pip_maintenance as pip_maintenance
import devclean.core.pnpm_maintenance as pnpm_maintenance
import devclean.core.uv_maintenance as uv_maintenance
from devclean.core.cleanup_catalog import KnownCleanupRoot
from devclean.core.pip_maintenance import PipCacheEntry, PipStorageInventory
from devclean.core.rule_schema import CleanupCategory, CleanupPolicy
from devclean.core.uv_maintenance import UvCacheEntry, UvStorageInventory
from devclean.core.vendor_cleanup_actions import (
    VendorCleanupKind,
    inventory_vendor_cleanup_candidates,
)
from devclean.ui.product_vendor_app import (
    _generic_vendor_skip_paths,
    _surfaceable_vendor_candidates,
    _vendor_row_id,
)

_MIB = 1024**2


def test_selective_vendor_inventory_does_not_walk_unrequested_providers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pip_cache = tmp_path / "pip"
    pip_cache.mkdir()
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(pip_cache, 5 * _MIB, True, False, True),)
        ),
    )

    def unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("unrequested provider inventory must not run")

    monkeypatch.setattr(uv_maintenance, "inventory_uv_storage", unexpected)
    monkeypatch.setattr(pnpm_maintenance, "inventory_pnpm_storage", unexpected)
    monkeypatch.setattr(go_maintenance, "inventory_go_storage", unexpected)
    monkeypatch.setattr(nuget_maintenance, "inventory_nuget_storage", unexpected)
    monkeypatch.setattr(conda_maintenance, "inventory_conda_storage", unexpected)

    inventory = inventory_vendor_cleanup_candidates(
        {},
        kinds=frozenset({VendorCleanupKind.PIP_CACHE_PURGE}),
    )

    assert len(inventory.candidates) == 1
    assert inventory.candidates[0].kind is VendorCleanupKind.PIP_CACHE_PURGE
    assert inventory.candidates[0].path == pip_cache


def test_product_safe_list_rejects_partial_gc_root_sizes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pip_cache = tmp_path / "pip"
    uv_cache = tmp_path / "uv"
    pip_cache.mkdir()
    uv_cache.mkdir()
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
            (UvCacheEntry(uv_cache, 500 * _MIB, True, False),)
        ),
    )

    inventory = inventory_vendor_cleanup_candidates(
        {},
        kinds=frozenset(
            {
                VendorCleanupKind.PIP_CACHE_PURGE,
                VendorCleanupKind.UV_CACHE_PRUNE,
            }
        ),
    )
    visible = _surfaceable_vendor_candidates(
        inventory.candidates,
        (Path(tmp_path.anchor),),
    )

    assert [candidate.kind for candidate in visible] == [
        VendorCleanupKind.PIP_CACHE_PURGE
    ]
    assert sum(candidate.observed_bytes for candidate in visible) == 5 * _MIB


def test_known_partial_gc_cache_roots_are_pruned_from_generic_file_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pip_cache = tmp_path / "pip"
    uv_cache = tmp_path / "uv"
    pnpm_store = tmp_path / "pnpm"
    conda_cache = tmp_path / "conda"
    unrelated = tmp_path / "other"
    for path in (pip_cache, uv_cache, pnpm_store, conda_cache, unrelated):
        path.mkdir()

    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(pip_cache, 5 * _MIB, True, False, True),)
        ),
    )
    inventory = inventory_vendor_cleanup_candidates(
        {},
        kinds=frozenset({VendorCleanupKind.PIP_CACHE_PURGE}),
    )
    roots = (
        KnownCleanupRoot(
            uv_cache,
            CleanupCategory.UV_CACHE,
            CleanupPolicy.REPORT_ONLY,
            "uv",
        ),
        KnownCleanupRoot(
            pnpm_store,
            CleanupCategory.PNPM_STORE,
            CleanupPolicy.REPORT_ONLY,
            "pnpm",
        ),
        KnownCleanupRoot(
            conda_cache,
            CleanupCategory.CONDA_CACHE,
            CleanupPolicy.REPORT_ONLY,
            "conda",
        ),
        KnownCleanupRoot(
            unrelated,
            CleanupCategory.OTHER,
            CleanupPolicy.REPORT_ONLY,
            "other",
        ),
    )

    skipped = set(
        _generic_vendor_skip_paths(
            roots,
            inventory.candidates,
            (Path(tmp_path.anchor),),
        )
    )

    assert str(pip_cache) in skipped
    assert str(uv_cache) in skipped
    assert str(pnpm_store) in skipped
    assert str(conda_cache) in skipped
    assert str(unrelated) not in skipped


def test_vendor_rows_use_non_path_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pip_cache = tmp_path / "pip"
    pip_cache.mkdir()
    monkeypatch.setattr(
        pip_maintenance,
        "inventory_pip_storage",
        lambda _environment=None: PipStorageInventory(
            (PipCacheEntry(pip_cache, 5 * _MIB, True, False, True),)
        ),
    )
    candidate = inventory_vendor_cleanup_candidates(
        {},
        kinds=frozenset({VendorCleanupKind.PIP_CACHE_PURGE}),
    ).candidates[0]

    row_id = _vendor_row_id(candidate)

    assert row_id.startswith("vendor:vendor_")
    assert row_id != str(candidate.path)
