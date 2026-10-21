from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from devclean.platform.windows import exact_cleanup as cleanup
from devclean.platform.windows.filesystem import read_file_metadata
from tests.test_exact_cleanup_safety import _metadata


@pytest.mark.parametrize("fail_leaf", [False, True])
def test_active_ancestors_stay_pinned_until_their_children_finish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_leaf: bool,
) -> None:
    root = str(tmp_path / "selected")
    child = str(Path(root) / "child")
    grandchild = str(Path(child) / "nested")
    leaf = str(Path(grandchild) / "file.bin")
    open_handles: set[str] = set()
    deleted: list[str] = []

    def open_directory(path: str) -> str:
        open_handles.add(path)
        return path

    def entries(
        path: str, boundary: str, *, already_pinned: bool = False
    ) -> tuple[tuple[str, cleanup._EntryKind, int], ...]:
        return {
            root: ((child, cleanup._EntryKind.DIRECTORY, 0),),
            child: ((grandchild, cleanup._EntryKind.DIRECTORY, 0),),
            grandchild: ((leaf, cleanup._EntryKind.FILE, 4),),
        }[path]

    def delete(handle: str, path: str) -> None:
        if path == leaf:
            assert child in open_handles and grandchild in open_handles
            if fail_leaf:
                raise OSError("injected leaf failure")
        deleted.append(path)

    monkeypatch.setattr(cleanup, "_open_exact_directory_for_mutation", open_directory)
    monkeypatch.setattr(cleanup, "_open_leaf_for_delete", lambda path: path)
    monkeypatch.setattr(cleanup, "_close_handle", lambda handle: open_handles.discard(handle))
    monkeypatch.setattr(cleanup, "_final_path", lambda handle: handle)
    monkeypatch.setattr(
        cleanup, "read_file_metadata_handle", lambda _handle: _metadata(directory=True)
    )
    monkeypatch.setattr(cleanup, "_list_purge_entries", entries)
    monkeypatch.setattr(cleanup, "_set_delete_disposition", delete)
    if fail_leaf:
        with pytest.raises(OSError, match="injected"):
            cleanup._purge_tree_contents(root, root, cleanup._TreePurgeState(), None, None)
    else:
        assert cleanup._purge_tree_contents(root, root, cleanup._TreePurgeState(), None, None)
        assert deleted == [leaf, grandchild, child]
    assert not open_handles


def _native_arguments(
    root: Path,
) -> tuple[cleanup.ExactDirectorySnapshot, cleanup.ExactRootBoundary]:
    target = read_file_metadata(root)
    parent = read_file_metadata(root.parent)
    assert target.volume_serial is not None and target.file_id is not None
    assert target.file_id_kind is not None and target.creation_time_ns is not None
    assert parent.volume_serial is not None and parent.file_id is not None
    assert parent.file_id_kind is not None
    return (
        cleanup.ExactDirectorySnapshot(
            target.volume_serial,
            target.file_id,
            target.file_id_kind,
            target.creation_time_ns,
        ),
        cleanup.ExactRootBoundary(
            root.parent,
            parent.volume_serial,
            parent.file_id,
            parent.file_id_kind,
        ),
    )


@pytest.mark.skipif(os.name != "nt", reason="requires real Windows directory sharing semantics")
def test_windows_blocks_rename_after_enumeration_until_child_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selected"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "same.bin").write_bytes(b"cache")
    victim = tmp_path / "keep.bin"
    victim.write_bytes(b"protected")
    original_list = cleanup._list_purge_entries
    checked: list[str] = []

    def list_then_try_swap(
        directory: str, boundary: str, *, already_pinned: bool = False
    ) -> tuple[tuple[str, cleanup._EntryKind, int], ...]:
        entries = original_list(directory, boundary, already_pinned=already_pinned)
        if Path(directory) == nested:
            with pytest.raises(OSError):
                nested.rename(root / "old-nested")
            checked.append(directory)
        return entries

    monkeypatch.setattr(cleanup, "_list_purge_entries", list_then_try_swap)
    snapshot, boundary = _native_arguments(root)
    result = cleanup.purge_exact_directory_tree(root, snapshot, boundary)
    assert checked
    assert result.completed and result.root_absent
    assert victim.read_bytes() == b"protected"


@pytest.mark.skipif(os.name != "nt", reason="requires Windows junctions and handle deletion")
def test_windows_refuses_junction_inserted_before_nested_directory_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "selected"
    nested = root / "nested"
    nested.mkdir(parents=True)
    (nested / "same.bin").write_bytes(b"cache")
    victim = tmp_path / "protected"
    victim.mkdir()
    (victim / "same.bin").write_bytes(b"protected")
    original_list = cleanup._list_purge_entries

    def list_then_swap(
        directory: str, boundary: str, *, already_pinned: bool = False
    ) -> tuple[tuple[str, cleanup._EntryKind, int], ...]:
        entries = original_list(directory, boundary, already_pinned=already_pinned)
        if Path(directory) == root:
            nested.rename(root / "old-nested")
            subprocess.run(
                ["cmd.exe", "/d", "/c", "mklink", "/J", str(nested), str(victim)],
                check=True,
                capture_output=True,
            )
        return entries

    monkeypatch.setattr(cleanup, "_list_purge_entries", list_then_swap)
    snapshot, boundary = _native_arguments(root)
    with pytest.raises(cleanup.ExactCleanupError, match="reparse/cloud"):
        cleanup.purge_exact_directory_tree(root, snapshot, boundary)
    assert (victim / "same.bin").read_bytes() == b"protected"
    # A failed purge must release all pins, allowing the test to remove the link.
    nested.rmdir()


def test_public_purge_rejects_a_leaf_resolved_to_a_protected_sibling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = str(tmp_path / "selected")
    leaf = str(Path(root) / "entry.bin")
    victim = str(tmp_path / "protected.bin")
    monkeypatch.setattr(cleanup, "_open_boundary", lambda _boundary: ("outer", str(tmp_path)))
    monkeypatch.setattr(cleanup, "_open_exact_directory_for_mutation", lambda _path: root)
    monkeypatch.setattr(cleanup, "_open_leaf_for_delete", lambda _path: leaf)
    monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)
    monkeypatch.setattr(
        cleanup, "read_file_metadata_handle", lambda _handle: _metadata(directory=True)
    )
    monkeypatch.setattr(cleanup, "_final_path", lambda handle: victim if handle == leaf else handle)
    monkeypatch.setattr(
        cleanup,
        "_list_purge_entries",
        lambda *args, **kwargs: ((leaf, cleanup._EntryKind.FILE, 4),),
    )
    mutations: list[str] = []
    monkeypatch.setattr(
        cleanup, "_set_delete_disposition", lambda _handle, path: mutations.append(path)
    )
    with pytest.raises(cleanup.ExactCleanupError, match="escaped"):
        cleanup.purge_exact_directory_tree(
            Path(root),
            cleanup.ExactDirectorySnapshot(1, "1" * 32, "file_id_128", 1),
            cleanup.ExactRootBoundary(tmp_path, 1, "2" * 32, "file_id_128"),
        )
    assert not mutations
