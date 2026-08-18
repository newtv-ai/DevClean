from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import devclean.core.vcpkg_maintenance as vcpkg_maintenance
from devclean.core.vcpkg_maintenance import (
    VcpkgStorageKind,
    clean_vcpkg_storage,
    inspect_vcpkg_root,
)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "vcpkg"
    root.mkdir()
    (root / ".vcpkg-root").write_text("", encoding="utf-8")
    executable = root / ("vcpkg.exe" if os.name == "nt" else "vcpkg")
    executable.write_text("stub", encoding="utf-8")
    return root


def _version_ok(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["vcpkg", "version"],
        returncode=0,
        stdout="vcpkg package management program version 2026-08-01\n",
        stderr="",
    )


def _patch_version_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "devclean.core.vcpkg_maintenance.subprocess.run",
        _version_ok,
    )


def test_inspect_requires_vcpkg_root_marker(tmp_path: Path) -> None:
    ordinary = tmp_path / "ordinary"
    ordinary.mkdir()
    executable = ordinary / ("vcpkg.exe" if os.name == "nt" else "vcpkg")
    executable.write_text("stub", encoding="utf-8")

    with pytest.raises(ValueError, match=r"\.vcpkg-root"):
        inspect_vcpkg_root(ordinary)


def test_inspect_splits_root_storage_and_default_binary_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    (root / "packages").mkdir()
    (root / "packages" / "a.bin").write_bytes(b"a" * 11)
    (root / "buildtrees").mkdir()
    (root / "buildtrees" / "b.bin").write_bytes(b"b" * 13)
    (root / "downloads").mkdir()
    (root / "downloads" / "c.bin").write_bytes(b"c" * 17)
    local = tmp_path / "local"
    archives = local / "vcpkg" / "archives"
    archives.mkdir(parents=True)
    (archives / "cache.zip").write_bytes(b"z" * 19)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.delenv("VCPKG_DEFAULT_BINARY_CACHE", raising=False)
    _patch_version_run(monkeypatch)

    inventory = inspect_vcpkg_root(root)

    by_kind = {entry.kind: entry for entry in inventory.entries}
    assert by_kind[VcpkgStorageKind.PACKAGES].logical_bytes == 11
    assert by_kind[VcpkgStorageKind.BUILDTREES].logical_bytes == 13
    assert by_kind[VcpkgStorageKind.DOWNLOADS].logical_bytes == 17
    assert by_kind[VcpkgStorageKind.DEFAULT_BINARY_CACHE].logical_bytes == 19
    assert by_kind[VcpkgStorageKind.DEFAULT_BINARY_CACHE].executable is False
    assert inventory.version.startswith("vcpkg package management program version")


def test_buildtrees_reason_warns_about_editable_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    (root / "buildtrees").mkdir()
    _patch_version_run(monkeypatch)

    inventory = inspect_vcpkg_root(root)
    entry = next(
        item for item in inventory.entries if item.kind is VcpkgStorageKind.BUILDTREES
    )

    assert "--editable" in entry.reason
    assert entry.executable


def test_binary_cache_cannot_enter_direct_cleanup(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)

    with pytest.raises(ValueError, match="仅报告"):
        clean_vcpkg_storage(root, VcpkgStorageKind.DEFAULT_BINARY_CACHE)


def test_cleanup_refuses_active_build(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    target = root / "packages"
    target.mkdir()
    (target / "keep.bin").write_bytes(b"x")
    _patch_version_run(monkeypatch)
    monkeypatch.setattr(vcpkg_maintenance, "vcpkg_activity_running", lambda: True)

    with pytest.raises(RuntimeError, match="构建活动"):
        clean_vcpkg_storage(root, VcpkgStorageKind.PACKAGES)

    assert (target / "keep.bin").exists()


def test_cleanup_uses_exact_tree_purge_and_reports_reclaimed_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    target = root / "downloads"
    target.mkdir()
    (target / "asset.zip").write_bytes(b"x" * 23)
    _patch_version_run(monkeypatch)
    monkeypatch.setattr(vcpkg_maintenance, "vcpkg_activity_running", lambda: False)
    monkeypatch.setattr(vcpkg_maintenance, "is_local_fixed_path", lambda path: True)
    monkeypatch.setattr(
        vcpkg_maintenance,
        "_exact_root_boundary",
        lambda path: object(),
    )
    monkeypatch.setattr(
        vcpkg_maintenance,
        "_exact_directory_snapshot",
        lambda path, label: object(),
    )

    def purge(path: Path, expected: object, boundary: object) -> object:
        for child in path.iterdir():
            child.unlink()
        path.rmdir()
        return type("Result", (), {"completed": True, "root_absent": True})()

    monkeypatch.setattr(vcpkg_maintenance, "purge_exact_directory_tree", purge)

    result = clean_vcpkg_storage(root, VcpkgStorageKind.DOWNLOADS)

    assert result.before_bytes == 23
    assert result.after_bytes == 0
    assert result.reclaimed_bytes == 23
    assert not target.exists()


def test_cleanup_refuses_non_local_storage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = _root(tmp_path)
    target = root / "packages"
    target.mkdir()
    _patch_version_run(monkeypatch)
    monkeypatch.setattr(vcpkg_maintenance, "vcpkg_activity_running", lambda: False)
    monkeypatch.setattr(vcpkg_maintenance, "is_local_fixed_path", lambda path: False)

    with pytest.raises(RuntimeError, match="本机固定磁盘"):
        clean_vcpkg_storage(root, VcpkgStorageKind.PACKAGES)
