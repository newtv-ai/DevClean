from __future__ import annotations

import os
from pathlib import Path

import pytest

from devclean.platform.windows import known_folders
from devclean.platform.windows.filesystem import FileSystemMetadata
from devclean.platform.windows.known_folders import CanonicalCleanupKind


def _directory_metadata() -> FileSystemMetadata:
    return FileSystemMetadata(
        is_directory=True,
        logical_size=0,
        allocation_size=0,
        volume_serial=42,
        file_id="ab" * 16,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=16,
        reparse_tag=None,
        is_reparse_point=False,
        is_cloud_placeholder=False,
        creation_time_ns=100,
        last_write_time_ns=200,
    )


def _patch_windows_roots(
    monkeypatch: pytest.MonkeyPatch,
    *,
    local_appdata: Path,
    temp: Path,
) -> None:
    monkeypatch.setattr(known_folders, "_windows_local_appdata", lambda: local_appdata)
    monkeypatch.setattr(known_folders, "_windows_temp_path2", lambda: temp)
    monkeypatch.setattr(known_folders, "is_local_fixed_path", lambda _path: True)
    monkeypatch.setattr(
        known_folders, "read_file_metadata", lambda _path: _directory_metadata()
    )


def test_canonical_roots_require_exact_known_folder_structure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "Local"
    temp = local / "Temp"
    crash = local / "CrashDumps"
    temp.mkdir(parents=True)
    crash.mkdir()
    _patch_windows_roots(monkeypatch, local_appdata=local, temp=temp)
    roots = known_folders.canonical_permanent_cleanup_roots()
    assert [(root.path, root.kind) for root in roots] == [
        (temp, CanonicalCleanupKind.USER_TEMP),
        (crash, CanonicalCleanupKind.CRASH_DUMPS),
    ]


def test_redirected_temp_outside_local_appdata_never_grants_permanent_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "Local"
    local.mkdir()
    (local / "CrashDumps").mkdir()
    redirected = tmp_path / "project"
    redirected.mkdir()
    _patch_windows_roots(monkeypatch, local_appdata=local, temp=redirected)
    roots = known_folders.canonical_permanent_cleanup_roots()
    assert all(root.kind is not CanonicalCleanupKind.USER_TEMP for root in roots)
    assert all(root.path != redirected for root in roots)


def test_missing_or_reparse_root_disables_permanent_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local = tmp_path / "Local"
    temp = local / "Temp"
    temp.mkdir(parents=True)
    _patch_windows_roots(monkeypatch, local_appdata=local, temp=temp)
    monkeypatch.setattr(known_folders, "is_local_fixed_path", lambda _path: False)
    assert known_folders.canonical_permanent_cleanup_roots() == ()


@pytest.mark.skipif(os.name != "nt", reason="Windows Known Folder provenance")
def test_real_api_provenance_never_follows_process_temp_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    redirected = tmp_path / "project-temp"
    redirected.mkdir()
    monkeypatch.setenv("TEMP", str(redirected))
    monkeypatch.setenv("TMP", str(redirected))
    roots = known_folders.canonical_permanent_cleanup_roots()
    assert all(root.path != redirected for root in roots)
    for root in roots:
        assert root.path.is_dir()
