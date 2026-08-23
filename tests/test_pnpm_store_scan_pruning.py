from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

import pytest

import devclean.core.cleanup_catalog as cleanup_catalog
from devclean.core.cleanup_catalog import KnownCleanupRoot
from devclean.core.rule_schema import CleanupCategory, CleanupPolicy
from devclean.ui.product_vendor_app import _generic_vendor_skip_paths


@pytest.mark.skipif(os.name != "nt", reason="Windows pnpm root semantics")
def test_pnpm_store_metadata_survives_application_root_replacement_and_prunes_only_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    localappdata = tmp_path / "Local"
    pnpm_home = localappdata / "pnpm"
    store = pnpm_home / "store"
    cache = localappdata / "pnpm-cache"
    state = localappdata / "pnpm-state"
    for path in (pnpm_home, store, cache, state):
        path.mkdir(parents=True, exist_ok=True)

    environment = {
        "USERPROFILE": str(tmp_path / "User"),
        "LOCALAPPDATA": str(localappdata),
        "APPDATA": str(tmp_path / "Roaming"),
        "TEMP": str(tmp_path / "Temp"),
        "PNPM_HOME": str(pnpm_home),
    }

    # Reproduce the catalog collision: packaged scan-rules.json has already
    # inserted the exact store as PNPM_STORE, then application_scan_roots reports
    # the same path and replace_existing=True refreshes its semantic metadata.
    accepted = [
        KnownCleanupRoot(
            store,
            CleanupCategory.PNPM_STORE,
            CleanupPolicy.REPORT_ONLY,
            "packaged pnpm store",
        )
    ]
    seen = {os.path.normcase(os.path.normpath(str(store)))}

    monkeypatch.setattr(
        cleanup_catalog,
        "application_scan_roots",
        lambda _environment=None: (
            PureWindowsPath(str(store)),
            PureWindowsPath(str(cache)),
            PureWindowsPath(str(state)),
            PureWindowsPath(str(pnpm_home)),
        ),
    )
    monkeypatch.setattr(cleanup_catalog, "application_roots", lambda _environment=None: ())
    monkeypatch.setattr(
        cleanup_catalog,
        "audited_dynamic_tool_roots",
        lambda _environment=None: (),
    )
    monkeypatch.setattr(cleanup_catalog, "is_local_fixed_path", lambda _path: True)

    cleanup_catalog._append_application_roots(accepted, seen, environment)

    by_path = {
        os.path.normcase(os.path.normpath(str(root.path))): root for root in accepted
    }
    store_root = by_path[os.path.normcase(os.path.normpath(str(store)))]
    assert store_root.category is CleanupCategory.PNPM_STORE
    assert store_root.policy is CleanupPolicy.REPORT_ONLY
    assert "pnpm store" in store_root.label

    for path in (cache, state, pnpm_home):
        root = by_path[os.path.normcase(os.path.normpath(str(path)))]
        assert root.category is not CleanupCategory.PNPM_STORE

    skipped = set(
        _generic_vendor_skip_paths(
            tuple(accepted),
            (),
            (Path(tmp_path.anchor),),
        )
    )
    assert str(store) in skipped
    assert str(cache) not in skipped
    assert str(state) not in skipped
    assert str(pnpm_home) not in skipped
