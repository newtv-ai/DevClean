"""Temporary folders cleaned as one item (devclean.core.temp_folders).

Everything runs on folders inside ``tmp_path``.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

from devclean.core.cleanup_catalog import CleanupCategory, CleanupPolicy, KnownCleanupRoot
from devclean.core.temp_folders import TempFolderKind, temp_folder_kind
from devclean.core.user_rules import default_rules
from devclean.platform.windows.exact_cleanup import (
    ExactCleanupError,
    ExactDirectorySnapshot,
    ExactRootBoundary,
    purge_directory_tree_skipping_in_use,
    purge_temp_folder_contents,
)
from devclean.platform.windows.filesystem import read_file_metadata


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    for name, value in {
        "USERPROFILE": home,
        "LOCALAPPDATA": local,
        "APPDATA": home / "AppData" / "Roaming",
        "TEMP": local / "Temp",
        "TMP": local / "Temp",
        "PROGRAMDATA": tmp_path / "ProgramData",
        "SYSTEMROOT": tmp_path / "Windows",
        "WINDIR": tmp_path / "Windows",
    }.items():
        monkeypatch.setenv(name, str(value))
    for name in ("PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432"):
        monkeypatch.delenv(name, raising=False)
    return home


def test_only_windows_user_and_application_temp_folders_count(
    profile: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keep = default_rules().keep.classification
    local = profile / "AppData" / "Local"

    def kind(path: Path, roots: tuple[KnownCleanupRoot, ...] = ()) -> TempFolderKind | None:
        return temp_folder_kind(path, roots, keep)

    assert kind(local / "Temp") is TempFolderKind.WINDOWS_OR_USER
    assert kind(tmp_path / "Windows" / "Temp") is TempFolderKind.WINDOWS_OR_USER
    assert kind(local / "Vendor" / "tmp") is TempFolderKind.APPLICATION
    assert kind(profile / "AppData" / "Roaming" / "Tool" / "Temp") is TempFolderKind.APPLICATION
    assert kind(profile / "AppData" / "LocalLow" / "Game" / "temp") is TempFolderKind.APPLICATION
    # Inside %TEMP%: part of that folder's item, not one of its own.
    assert kind(local / "Temp" / "build" / "tmp") is None
    # Anywhere else a temp/tmp folder is classified file by file as before.
    assert kind(profile / "source" / "app" / "tmp") is None
    assert kind(profile / "Documents" / "temp") is None
    assert kind(tmp_path / "ProgramData" / "Vendor" / "Temp") is None
    assert kind(local / "Vendor" / "cache") is None
    # Audited application storage keeps its own rules.
    vendor = KnownCleanupRoot(
        path=local / "Vendor",
        category=CleanupCategory.OTHER,
        policy=CleanupPolicy.REPORT_ONLY,
        label="vendor",
    )
    assert kind(local / "Vendor" / "tmp", (vendor,)) is None
    # A deny-listed system root never counts, whatever its name.
    assert kind(Path(tmp_path.anchor) / "ProgramData" / "App" / "Temp") is None
    # %TEMP% pointing somewhere broad is not trusted.
    monkeypatch.setenv("TEMP", str(profile))
    assert kind(profile) is None


def _arguments(folder: Path, boundary: Path) -> tuple[ExactDirectorySnapshot, ExactRootBoundary]:
    target = read_file_metadata(folder)
    outer = read_file_metadata(boundary)
    assert target.volume_serial is not None and target.file_id is not None
    assert target.file_id_kind is not None and target.creation_time_ns is not None
    assert outer.volume_serial is not None and outer.file_id is not None
    assert outer.file_id_kind is not None
    return (
        ExactDirectorySnapshot(
            target.volume_serial, target.file_id, target.file_id_kind, target.creation_time_ns
        ),
        ExactRootBoundary(boundary, outer.volume_serial, outer.file_id, outer.file_id_kind),
    )


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_a_file_another_program_holds_open_is_left_and_the_rest_goes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "Temp"
    busy = _write(folder / "busy.log", b"held")
    free = _write(folder / "free.log", b"go")
    nested = _write(folder / "sub" / "deeper" / "x.bin", b"123")
    snapshot, boundary = _arguments(folder, tmp_path)
    holder: Any = None
    if os.name == "nt":
        # Python opens files without delete sharing, as most programs do.
        holder = busy.open("rb")
    else:
        real_unlink = os.unlink

        def unlink(name: Any, *, dir_fd: int | None = None) -> None:
            if name == "busy.log":
                raise OSError(errno.EBUSY, "held by another program", name)
            real_unlink(name, dir_fd=dir_fd)

        monkeypatch.setattr(os, "unlink", unlink)
    try:
        result = purge_temp_folder_contents(
            folder, snapshot, boundary, keep=lambda *_entry: None
        )
    finally:
        if holder is not None:
            holder.close()

    assert result.completed
    assert (result.files_removed, result.bytes_removed) == (2, 5)
    assert (result.in_use, result.denied, result.kept) == (1, 0, 0)
    assert result.examples == ((str(busy), "正被程序使用"),)
    assert busy.exists() and not free.exists() and not nested.exists()
    assert not (folder / "sub").exists()
    assert folder.is_dir()  # the folder itself stays


def test_entries_another_rule_claims_stay_with_their_folders(tmp_path: Path) -> None:
    folder = tmp_path / "Temp"
    claimed = _write(folder / "app" / "state" / "keep.json")
    junk = _write(folder / "app" / "junk.tmp")
    snapshot, boundary = _arguments(folder, tmp_path)

    def keep(path: str, is_directory: bool, _size: int, _mtime: int | None) -> str | None:
        return "应用规则管理它" if path == str(claimed) else None

    result = purge_temp_folder_contents(folder, snapshot, boundary, keep=keep)

    assert result.kept == 1 and result.files_removed == 1
    assert claimed.exists() and not junk.exists()
    assert (folder / "app" / "state").is_dir()


def test_a_link_inside_is_removed_as_a_link_and_its_target_survives(tmp_path: Path) -> None:
    folder = tmp_path / "Temp"
    folder.mkdir()
    target = _write(tmp_path / "outside" / "precious.txt", b"keep")
    (folder / "link").symlink_to(target.parent, target_is_directory=True)
    snapshot, boundary = _arguments(folder, tmp_path)

    result = purge_temp_folder_contents(folder, snapshot, boundary, keep=lambda *_entry: None)

    assert result.links_removed == 1
    assert not os.path.lexists(folder / "link")
    assert target.read_bytes() == b"keep"


def test_a_folder_replaced_since_the_scan_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "Temp"
    _write(folder / "a.tmp")
    snapshot, boundary = _arguments(folder, tmp_path)
    folder.rename(tmp_path / "Temp-old")
    _write(folder / "victim.txt", b"protected")

    with pytest.raises(ExactCleanupError, match="changed"):
        purge_temp_folder_contents(folder, snapshot, boundary, keep=lambda *_entry: None)
    assert (folder / "victim.txt").read_bytes() == b"protected"


def _hold(path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make *path* undeletable as if another program held it open."""

    if os.name == "nt":
        return path.open("rb")  # Python opens files without delete sharing
    real_unlink = os.unlink

    def unlink(name: Any, *, dir_fd: int | None = None) -> None:
        if name == path.name:
            raise OSError(errno.EBUSY, "held by another program", name)
        real_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    return None


def test_a_cache_tree_keeps_only_what_a_running_program_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tree = tmp_path / "Cache"
    busy = _write(tree / "data_1", b"index")
    loose = [_write(tree / "f_000001", b"a"), _write(tree / "js" / "f_000002", b"bb")]
    snapshot, boundary = _arguments(tree, tmp_path)
    holder = _hold(busy, monkeypatch)
    try:
        result = purge_directory_tree_skipping_in_use(tree, snapshot, boundary)
    finally:
        if holder is not None:
            holder.close()

    assert result.completed and not result.root_absent
    assert (result.files_removed, result.bytes_removed, result.in_use) == (2, 3, 1)
    assert busy.exists() and not any(path.exists() for path in loose)
    assert not (tree / "js").exists()


def test_a_cache_tree_nothing_holds_goes_whole(tmp_path: Path) -> None:
    tree = tmp_path / "Code Cache"
    _write(tree / "js" / "index", b"1")
    _write(tree / "wasm" / "index", b"2")
    snapshot, boundary = _arguments(tree, tmp_path)

    result = purge_directory_tree_skipping_in_use(tree, snapshot, boundary)

    assert result.completed and result.root_absent and result.in_use == 0
    assert not tree.exists()
