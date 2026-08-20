from __future__ import annotations

import shutil
from pathlib import Path

import pytest

import devclean.core.unity_upm_maintenance as upm
from devclean.core.unity_upm_maintenance import (
    UnityUpmLane,
    UnityUpmRootOrigin,
    UnityUpmStorageKind,
    delete_unity_upm_legacy_packages,
    inventory_unity_upm_storage,
)
from devclean.platform.windows.exact_cleanup import (
    DirectoryPurgeResult,
    ExactDirectorySnapshot,
    ExactRootBoundary,
)


def _write(path: Path, size: int = 1) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _entry(
    inventory: upm.UnityUpmInventory,
    kind: UnityUpmStorageKind,
    path: Path,
) -> upm.UnityUpmStorageEntry:
    return next(item for item in inventory.entries if item.kind is kind and item.path == path)


def test_default_unity6_global_cache_is_vendor_managed(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    root = local / "Unity" / "cache" / "upm"
    _write(root / "db" / "registry" / "pkg.tgz", 31)
    _write(root / "packages" / "legacy" / "package.json", 17)

    inventory = inventory_unity_upm_storage({"LOCALAPPDATA": str(local)})

    assert inventory.active_root == root
    assert inventory.active_db == root / "db"
    assert inventory.db_max_bytes == 10_000_000_000
    assert inventory.db_max_source is UnityUpmRootOrigin.DEFAULT
    db = _entry(inventory, UnityUpmStorageKind.DB, root / "db")
    assert db.logical_bytes == 31
    assert db.active
    assert db.lane is UnityUpmLane.UNITY_MANAGED
    assert not db.deletable
    packages = _entry(
        inventory,
        UnityUpmStorageKind.LEGACY_PACKAGES,
        root / "packages",
    )
    assert packages.logical_bytes == 17
    assert packages.lane is UnityUpmLane.USER_REVIEW
    assert packages.deletable


def test_user_config_override_keeps_old_default_root_visible(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    profile = tmp_path / "profile"
    default_root = local / "Unity" / "cache" / "upm"
    configured_root = tmp_path / "ConfiguredUpm"
    _write(default_root / "db" / "old.tgz", 7)
    _write(configured_root / "db" / "current.tgz", 11)
    profile.mkdir()
    (profile / ".upmconfig.toml").write_text(
        f'cacheRoot = "{configured_root.as_posix()}"\nmaxCacheSize = 5000000000\n',
        encoding="utf-8",
    )

    inventory = inventory_unity_upm_storage(
        {
            "LOCALAPPDATA": str(local),
            "USERPROFILE": str(profile),
        }
    )

    assert inventory.active_root == configured_root
    assert inventory.active_db == configured_root / "db"
    assert inventory.db_max_bytes == 5_000_000_000
    assert inventory.db_max_source is UnityUpmRootOrigin.USER_CONFIG
    roots = {root.path: root for root in inventory.roots}
    assert roots[configured_root].active
    assert roots[configured_root].origin is UnityUpmRootOrigin.USER_CONFIG
    assert not roots[default_root].active
    old_db = _entry(inventory, UnityUpmStorageKind.DB, default_root / "db")
    assert old_db.lane is UnityUpmLane.REPORT_ONLY
    assert not old_db.deletable


def test_environment_overrides_user_config_and_db_path(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    profile = tmp_path / "profile"
    configured_root = tmp_path / "Configured"
    environment_root = tmp_path / "Environment"
    database = tmp_path / "SeparateDb"
    git_lfs = tmp_path / "SeparateLfs"
    profile.mkdir()
    (profile / ".upmconfig.toml").write_text(
        f'cacheRoot = "{configured_root.as_posix()}"\nmaxCacheSize = 7000000000\n',
        encoding="utf-8",
    )
    _write(database / "pkg.tgz", 13)
    _write(git_lfs / "objects" / "blob", 19)

    inventory = inventory_unity_upm_storage(
        {
            "LOCALAPPDATA": str(local),
            "USERPROFILE": str(profile),
            "UPM_CACHE_ROOT": str(environment_root),
            "UPM_NPM_CACHE_PATH": str(database),
            "UPM_MAX_CACHE_SIZE": "3000000000",
            "UPM_GIT_LFS_CACHE_PATH": str(git_lfs),
        }
    )

    assert inventory.active_root == environment_root
    assert inventory.active_db == database
    assert inventory.db_max_bytes == 3_000_000_000
    assert inventory.db_max_source is UnityUpmRootOrigin.ENVIRONMENT
    db = _entry(inventory, UnityUpmStorageKind.DB, database)
    assert db.active
    assert db.logical_bytes == 13
    lfs = _entry(inventory, UnityUpmStorageKind.GIT_LFS, git_lfs)
    assert lfs.active
    assert lfs.logical_bytes == 19
    roots = {root.path: root for root in inventory.roots}
    assert roots[environment_root].active
    assert roots[environment_root].origin is UnityUpmRootOrigin.ENVIRONMENT
    assert configured_root in roots


def test_git_lfs_cache_is_report_only_even_when_enabled(tmp_path: Path) -> None:
    local = tmp_path / "Local"
    root = local / "Unity" / "cache" / "upm"
    _write(root / "git-lfs" / "objects" / "blob", 23)

    inventory = inventory_unity_upm_storage(
        {
            "LOCALAPPDATA": str(local),
            "UPM_ENABLE_GIT_LFS_CACHE": "1",
        }
    )

    lfs = _entry(inventory, UnityUpmStorageKind.GIT_LFS, root / "git-lfs")
    assert lfs.active
    assert lfs.lane is UnityUpmLane.REPORT_ONLY
    assert not lfs.deletable


def test_invalid_relative_cache_root_is_refused(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".upmconfig.toml").write_text(
        'cacheRoot = "relative/cache"\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="绝对路径"):
        inventory_unity_upm_storage({"USERPROFILE": str(profile)})


def test_malformed_user_config_is_not_silently_ignored(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / ".upmconfig.toml").write_text(
        'cacheRoot = "unterminated\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="无法安全读取"):
        inventory_unity_upm_storage({"USERPROFILE": str(profile)})


def test_delete_legacy_packages_uses_exact_tree_purge_and_preserves_siblings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Unity" / "cache" / "upm"
    packages = root / "packages"
    _write(packages / "legacy" / "package.json", 29)
    db = _write(root / "db" / "keep.tgz", 37)
    lfs = _write(root / "git-lfs" / "keep.bin", 41)
    boundary = ExactRootBoundary(root, 1, "root", "file_id_128")
    snapshot = ExactDirectorySnapshot(1, "packages", "file_id_128", 123)
    seen: dict[str, object] = {}

    monkeypatch.setattr(upm, "unity_package_manager_running", lambda: False)
    monkeypatch.setattr(upm, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(upm, "_exact_root_boundary", lambda path: boundary)
    monkeypatch.setattr(
        upm,
        "_exact_directory_snapshot",
        lambda path, label: snapshot,
    )

    def fake_purge(
        path: Path,
        expected: ExactDirectorySnapshot,
        exact_boundary: ExactRootBoundary,
    ) -> DirectoryPurgeResult:
        seen["path"] = path
        seen["expected"] = expected
        seen["boundary"] = exact_boundary
        shutil.rmtree(path)
        return DirectoryPurgeResult(
            root_path=str(path),
            files_removed=1,
            links_removed=0,
            directories_removed=2,
            bytes_removed=29,
            root_absent=True,
            completed=True,
        )

    monkeypatch.setattr(upm, "purge_exact_directory_tree", fake_purge)

    result = delete_unity_upm_legacy_packages(
        root,
        {"LOCALAPPDATA": str(local)},
    )

    assert seen == {
        "path": packages,
        "expected": snapshot,
        "boundary": boundary,
    }
    assert result.before_bytes == 29
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 29
    assert not packages.exists()
    assert db.read_bytes() == b"x" * 37
    assert lfs.read_bytes() == b"x" * 41


def test_delete_refuses_arbitrary_packages_directory(tmp_path: Path) -> None:
    arbitrary = tmp_path / "arbitrary"
    _write(arbitrary / "packages" / "data.bin", 5)

    with pytest.raises(ValueError, match="不是当前可确认"):
        delete_unity_upm_legacy_packages(
            arbitrary,
            {"LOCALAPPDATA": str(tmp_path / "Local")},
        )

    assert (arbitrary / "packages" / "data.bin").exists()


def test_delete_refuses_non_local_or_reparsed_cache_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Unity" / "cache" / "upm"
    package = _write(root / "packages" / "legacy" / "data.bin", 5)
    monkeypatch.setattr(upm, "is_local_fixed_path", lambda path: False)

    with pytest.raises(ValueError, match="本地固定磁盘"):
        delete_unity_upm_legacy_packages(
            root,
            {"LOCALAPPDATA": str(local)},
        )

    assert package.exists()


def test_delete_refuses_while_unity_package_manager_is_running(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local = tmp_path / "Local"
    root = local / "Unity" / "cache" / "upm"
    _write(root / "packages" / "legacy" / "data.bin", 5)
    monkeypatch.setattr(upm, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(upm, "unity_package_manager_running", lambda: True)

    with pytest.raises(RuntimeError, match="正在运行"):
        delete_unity_upm_legacy_packages(
            root,
            {"LOCALAPPDATA": str(local)},
        )

    assert (root / "packages" / "legacy" / "data.bin").exists()
