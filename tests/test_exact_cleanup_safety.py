from __future__ import annotations

import os
import types
from collections.abc import Callable
from pathlib import Path

import pytest

import devclean.platform.windows.exact_cleanup as cleanup
from devclean.platform.windows.exact_cleanup import ExactCleanupError
from devclean.platform.windows.filesystem import FileSystemMetadata
from devclean.ui import app


def _metadata(*, directory: bool = False, reparse: bool = False) -> FileSystemMetadata:
    return FileSystemMetadata(
        is_directory=directory,
        logical_size=0,
        allocation_size=0,
        volume_serial=1,
        file_id="1" * 32,
        file_id_kind="file_id_128",
        link_count=1,
        attributes=0x10 if directory else 0,
        reparse_tag=0xA0000003 if reparse else None,
        is_reparse_point=reparse,
        is_cloud_placeholder=False,
        creation_time_ns=1,
        last_write_time_ns=1,
    )


def test_leaf_delete_revalidates_open_handle_inside_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    handle = object()
    monkeypatch.setattr(cleanup, "_open_leaf_for_delete", lambda _path: handle)
    monkeypatch.setattr(
        cleanup,
        "_require_handle_in_boundary",
        lambda actual, boundary, *, allow_equal: calls.append(
            ("boundary", (actual, boundary, allow_equal))
        ),
    )
    monkeypatch.setattr(
        cleanup,
        "_set_delete_disposition",
        lambda actual, target: calls.append(("delete", (actual, target))),
    )
    monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)

    cleanup._delete_leaf(r"C:\cache\x.bin", r"C:\cache")

    assert calls[0] == ("boundary", (handle, r"C:\cache", False))
    assert calls[1][0] == "delete"


def test_nested_reparse_is_refused_before_scandir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = object()
    monkeypatch.setattr(cleanup, "_open_exact_directory", lambda _path: handle)
    monkeypatch.setattr(
        cleanup,
        "read_file_metadata_handle",
        lambda _handle: _metadata(directory=True, reparse=True),
    )
    monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)
    monkeypatch.setattr(
        os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("scandir must not follow replaced junction")
        ),
    )

    with pytest.raises(ExactCleanupError, match="reparse/cloud"):
        cleanup._list_purge_entries(r"C:\cache\nested", r"C:\cache")


def test_already_pinned_root_does_not_open_second_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        cleanup,
        "_open_exact_directory",
        lambda _path: (_ for _ in ()).throw(AssertionError("selected root is already pinned")),
    )

    class EmptyScan:
        def __enter__(self) -> EmptyScan:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def __iter__(self) -> object:
            return iter(())

    monkeypatch.setattr(os, "scandir", lambda _path: EmptyScan())

    assert cleanup._list_purge_entries(r"C:\cache", r"C:\approved", already_pinned=True) == ()


def test_empty_directory_delete_revalidates_boundary_and_final_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    handle = object()
    calls: list[str] = []
    monkeypatch.setattr(cleanup, "_open_exact_directory_for_mutation", lambda _path: handle)
    monkeypatch.setattr(
        cleanup, "read_file_metadata_handle", lambda _handle: _metadata(directory=True)
    )
    monkeypatch.setattr(
        cleanup,
        "_require_handle_in_boundary",
        lambda _handle, _boundary, *, allow_equal: calls.append(f"boundary:{allow_equal}"),
    )
    monkeypatch.setattr(
        cleanup, "_require_final_path", lambda _handle, _path: calls.append("final")
    )
    monkeypatch.setattr(
        cleanup, "_set_delete_disposition", lambda _handle, _path: calls.append("delete")
    )
    monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)

    cleanup._delete_empty_directory(r"C:\cache\nested", r"C:\cache")

    assert calls == ["boundary:False", "final", "delete"]


def _purge_file(path: Path, root: Path) -> object:
    snapshot = cleanup.ExactFileSnapshot(0, 1, "a" * 32, "file_id_128", 1, 0, None, 1, 1)
    return cleanup.purge_exact_file(path, snapshot, cleanup.ExactRootBoundary(root, 1, "r", "k"))


def _recycle_file(path: Path, root: Path) -> object:
    snapshot = cleanup.ExactFileSnapshot(0, 1, "a" * 32, "file_id_128", 1, 0, None, 1, 1)
    boundary = cleanup.ExactRootBoundary(root, 1, "r", "k")
    return cleanup.recycle_exact_object(path, snapshot, boundary)


def _purge_tree(path: Path, root: Path) -> object:
    snapshot = cleanup.ExactDirectorySnapshot(1, "a" * 32, "file_id_128", 1)
    boundary = cleanup.ExactRootBoundary(root, 1, "r", "k")
    return cleanup.purge_exact_directory_tree(path, snapshot, boundary)


@pytest.mark.parametrize(
    ("mutate", "directory"),
    [(_purge_file, False), (_recycle_file, False), (_purge_tree, True)],
    ids=["permanent-file", "recycle-file", "directory-tree"],
)
def test_a_link_swapped_into_the_path_is_reported_as_a_boundary_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutate: Callable[[Path, Path], object],
    directory: bool,
) -> None:
    # After every earlier check, a parent folder became a link: the handle opens
    # a different object (other file id) outside the approved root. Both checks
    # refuse it; the reason must name the link, not "content changed".
    root = tmp_path / "approved"
    victim = os.path.normcase(str(tmp_path / "elsewhere" / "Setup.exe"))
    mutations: list[str] = []
    monkeypatch.setattr(cleanup, "os", types.SimpleNamespace(**{**vars(os), "name": "nt"}))
    monkeypatch.setattr(
        cleanup, "_open_boundary", lambda _boundary: ("root", os.path.normcase(str(root)))
    )
    monkeypatch.setattr(cleanup, "_open_exact_file", lambda _path: "opened")
    monkeypatch.setattr(cleanup, "_open_exact_directory_for_mutation", lambda _path: "opened")
    monkeypatch.setattr(
        cleanup, "read_file_metadata_handle", lambda _handle: _metadata(directory=directory)
    )
    monkeypatch.setattr(
        cleanup, "_final_path", lambda handle: victim if handle == "opened" else handle
    )
    monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)
    monkeypatch.setattr(
        cleanup, "_set_delete_disposition", lambda _handle, path: mutations.append(path)
    )
    monkeypatch.setattr(cleanup, "_shell_delete_to_recycle_bin", mutations.append)
    monkeypatch.setattr(cleanup, "_recycle_bin_item_count", lambda _path: None)

    with pytest.raises(ExactCleanupError, match="escaped") as refused:
        mutate(root / "c" / "Setup.exe", root)

    assert app._reason_of(refused.value) == "路径被替换成链接或离开了扫描范围，安全检查拒绝"  # noqa: RUF001
    assert mutations == []
