from __future__ import annotations

from pathlib import Path

import pytest

import devclean.core.unity_asset_store_maintenance as unity_assets
from devclean.core.unity_asset_store_maintenance import (
    UnityAssetStoreRootOrigin,
    delete_unity_asset_store_package,
    inventory_unity_asset_store,
)
from devclean.platform.windows.exact_cleanup import ExactMutationResult


def _write_package(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_inventory_discovers_default_asset_store_cache(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"
    package = _write_package(
        appdata / "Unity" / "Asset Store-5.x" / "Publisher" / "Product" / "A.unitypackage",
        31,
    )

    inventory = inventory_unity_asset_store({"APPDATA": str(appdata)})

    assert len(inventory.roots) == 1
    root = inventory.roots[0]
    assert root.origin is UnityAssetStoreRootOrigin.DEFAULT
    assert root.path == appdata / "Unity" / "Asset Store-5.x"
    assert root.exists
    assert root.package_count == 1
    assert root.package_bytes == 31
    assert inventory.package_bytes == 31
    assert inventory.packages[0].path == package
    assert inventory.packages[0].publisher == "Publisher"
    assert inventory.packages[0].user_review_required


def test_inventory_adds_environment_override_without_hiding_default(
    tmp_path: Path,
) -> None:
    appdata = tmp_path / "Roaming"
    default_package = _write_package(
        appdata / "Unity" / "Asset Store-5.x" / "Default" / "One" / "a.unitypackage",
        10,
    )
    override_parent = tmp_path / "AssetCache"
    override_package = _write_package(
        override_parent / "Asset Store-5.x" / "Vendor" / "Two" / "b.unitypackage",
        20,
    )

    inventory = inventory_unity_asset_store(
        {
            "APPDATA": str(appdata),
            "ASSETSTORE_CACHE_PATH": str(override_parent),
        }
    )

    assert {package.path for package in inventory.packages} == {
        default_package,
        override_package,
    }
    assert {root.origin for root in inventory.roots} == {
        UnityAssetStoreRootOrigin.DEFAULT,
        UnityAssetStoreRootOrigin.ENVIRONMENT,
    }


def test_environment_override_may_point_at_asset_store_directory(tmp_path: Path) -> None:
    root = tmp_path / "Asset Store-5.x"
    package = _write_package(root / "Vendor" / "Product" / "pkg.unitypackage", 23)

    inventory = inventory_unity_asset_store({"ASSETSTORE_CACHE_PATH": str(root)})

    assert len(inventory.roots) == 1
    assert inventory.roots[0].path == root
    assert inventory.packages[0].path == package


def test_user_selected_parent_is_normalized_and_deduplicated(tmp_path: Path) -> None:
    parent = tmp_path / "chosen"
    package = _write_package(
        parent / "Asset Store-5.x" / "Vendor" / "Product" / "pkg.unitypackage",
        9,
    )

    inventory = inventory_unity_asset_store(
        {"ASSETSTORE_CACHE_PATH": str(parent)},
        (parent, parent / "Asset Store-5.x"),
    )

    assert len(inventory.roots) == 1
    assert inventory.roots[0].origin is UnityAssetStoreRootOrigin.ENVIRONMENT
    assert inventory.packages[0].path == package


def test_inventory_ignores_non_package_files(tmp_path: Path) -> None:
    appdata = tmp_path / "Roaming"
    root = appdata / "Unity" / "Asset Store-5.x"
    _write_package(root / "Vendor" / "Product" / "keep.txt", 100)
    package = _write_package(root / "Vendor" / "Product" / "real.UNITYPACKAGE", 15)

    inventory = inventory_unity_asset_store({"APPDATA": str(appdata)})

    assert [entry.path for entry in inventory.packages] == [package]
    assert inventory.package_bytes == 15


def test_delete_refuses_package_outside_approved_cache(tmp_path: Path) -> None:
    root = tmp_path / "Asset Store-5.x"
    root.mkdir()
    outside = _write_package(tmp_path / "outside.unitypackage", 5)

    with pytest.raises(ValueError, match="不在已批准"):
        delete_unity_asset_store_package(root, outside)

    assert outside.exists()


def test_delete_refuses_non_unitypackage_file(tmp_path: Path) -> None:
    root = tmp_path / "Asset Store-5.x"
    root.mkdir()
    other = _write_package(root / "Vendor" / "Product" / "notes.txt", 5)

    with pytest.raises(ValueError, match="只允许删除"):
        delete_unity_asset_store_package(root, other)

    assert other.exists()


def test_delete_uses_exact_file_purge_and_preserves_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "Asset Store-5.x"
    selected = _write_package(root / "Vendor" / "Product" / "selected.unitypackage", 17)
    sibling = _write_package(root / "Vendor" / "Product" / "keep.unitypackage", 19)
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        unity_assets,
        "_exact_root_boundary",
        lambda path: object(),
    )
    monkeypatch.setattr(
        unity_assets,
        "_exact_file_snapshot",
        lambda path: type("Snapshot", (), {"logical_size": 17})(),
    )

    def fake_purge(path: Path, expected: object, boundary: object) -> ExactMutationResult:
        seen["path"] = path
        seen["expected"] = expected
        seen["boundary"] = boundary
        path.unlink()
        return ExactMutationResult(
            source_path=str(path),
            destination_path=None,
            source_name_absent=True,
            source_name_replaced=False,
            destination_matches=False,
        )

    monkeypatch.setattr(unity_assets, "purge_exact_file", fake_purge)

    result = delete_unity_asset_store_package(root, selected)

    assert seen["path"] == selected
    assert result.before_bytes == 17
    assert result.reclaimed_bytes == 17
    assert not selected.exists()
    assert sibling.read_bytes() == b"x" * 19


def test_delete_rejects_linked_cache_root(tmp_path: Path) -> None:
    target = tmp_path / "target" / "Asset Store-5.x"
    target.mkdir(parents=True)
    link = tmp_path / "Asset Store-5.x"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable on this test host")
    package = _write_package(target / "Vendor" / "Product" / "pkg.unitypackage", 8)

    with pytest.raises(ValueError, match="链接或 junction"):
        delete_unity_asset_store_package(link, package)
