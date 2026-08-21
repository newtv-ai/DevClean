from __future__ import annotations

import re
from pathlib import Path
from textwrap import dedent


path = Path("src/devclean/platform/windows/exact_cleanup.py")
text = path.read_text(encoding="utf-8")

simple = [
    (
        "            completed = _purge_tree_contents(\n                root_path, state, on_progress, is_cancelled\n            )\n",
        "            completed = _purge_tree_contents(\n                root_path, root_final, state, on_progress, is_cancelled\n            )\n",
    ),
    (
        "def _purge_tree_contents(\n    root_path: str,\n    state: _TreePurgeState,\n",
        "def _purge_tree_contents(\n    root_path: str,\n    boundary_final: str,\n    state: _TreePurgeState,\n",
    ),
    (
        "                _delete_empty_directory(current)\n",
        "                _delete_empty_directory(current, boundary_final)\n",
    ),
    (
        "        children = _list_purge_entries(current)\n",
        "        children = _list_purge_entries(\n            current, boundary_final, already_pinned=current == root_path\n        )\n",
    ),
    (
        "            _delete_leaf(child_path)\n",
        "            _delete_leaf(child_path, boundary_final)\n",
    ),
    (
        "def _delete_leaf(path: str) -> None:\n",
        "def _delete_leaf(path: str, boundary_final: str) -> None:\n",
    ),
    (
        "    handle = _open_leaf_for_delete(path)\n    try:\n        _set_delete_disposition(handle, path)\n",
        "    handle = _open_leaf_for_delete(path)\n    try:\n        _require_handle_in_boundary(handle, boundary_final, allow_equal=False)\n        _set_delete_disposition(handle, path)\n",
    ),
]
for old, new in simple:
    if old not in text:
        raise SystemExit(f"missing patch anchor: {old[:80]!r}")
    text = text.replace(old, new, 1)

new_list = dedent(
    '''
    def _list_purge_entries(
        directory: str, boundary_final: str, *, already_pinned: bool = False
    ) -> tuple[tuple[str, _EntryKind, int], ...]:
        # The selected tree root is already pinned by purge_exact_directory_tree.
        # Every nested directory is separately pinned immediately before traversal
        # so a pathname replacement cannot redirect scandir through a junction.
        handle: wintypes.HANDLE | None = None
        if not already_pinned:
            handle = _open_exact_directory(directory)
            metadata = read_file_metadata_handle(handle)
            if (
                not metadata.is_directory
                or metadata.is_reparse_point
                or metadata.is_cloud_placeholder
            ):
                _close_handle(handle)
                raise ExactCleanupError(
                    "selected tree directory became a reparse/cloud boundary"
                )
            try:
                _require_handle_in_boundary(handle, boundary_final, allow_equal=False)
                _require_final_path(handle, directory)
            except Exception:
                _close_handle(handle)
                raise
        try:
            entries: list[tuple[str, _EntryKind, int]] = []
            with os.scandir(_extended_path(directory)) as scan:
                for entry in scan:
                    try:
                        status = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    child = os.path.join(directory, entry.name)
                    attributes = getattr(status, "st_file_attributes", 0)
                    if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                        entries.append((child, _EntryKind.REPARSE_POINT, 0))
                    elif attributes & _FILE_ATTRIBUTE_DIRECTORY:
                        entries.append((child, _EntryKind.DIRECTORY, 0))
                    else:
                        entries.append((child, _EntryKind.FILE, status.st_size))
            return tuple(entries)
        finally:
            if handle is not None:
                _close_handle(handle)
    '''
).lstrip()
text, count = re.subn(
    r"def _list_purge_entries\(directory: str\) -> tuple\[tuple\[str, _EntryKind, int\], \.\.\.\]:\n.*?\n\ndef _delete_leaf",
    new_list + "\n\ndef _delete_leaf",
    text,
    count=1,
    flags=re.DOTALL,
)
if count != 1:
    raise SystemExit("could not replace purge entry traversal")

new_empty_delete = dedent(
    '''
    def _delete_empty_directory(path: str, boundary_final: str) -> None:
        handle = _open_exact_directory_for_mutation(path)
        try:
            metadata = read_file_metadata_handle(handle)
            if (
                not metadata.is_directory
                or metadata.is_reparse_point
                or metadata.is_cloud_placeholder
            ):
                raise ExactCleanupError("selected tree directory changed before deletion")
            _require_handle_in_boundary(handle, boundary_final, allow_equal=False)
            _require_final_path(handle, path)
            _set_delete_disposition(handle, path)
        finally:
            _close_handle(handle)
    '''
).lstrip()
text, count = re.subn(
    r"def _delete_empty_directory\(path: str\) -> None:\n.*?\n\ndef _open_boundary",
    new_empty_delete + "\n\ndef _open_boundary",
    text,
    count=1,
    flags=re.DOTALL,
)
if count != 1:
    raise SystemExit("could not replace nested directory delete")
path.write_text(text, encoding="utf-8")

Path("tests/test_exact_cleanup_safety.py").write_text(
    dedent(
        '''
        from __future__ import annotations

        import os

        import pytest

        import devclean.platform.windows.exact_cleanup as cleanup
        from devclean.platform.windows.exact_cleanup import ExactCleanupError
        from devclean.platform.windows.filesystem import FileSystemMetadata


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

            cleanup._delete_leaf(r"C:\\cache\\x.bin", r"C:\\cache")

            assert calls[0] == ("boundary", (handle, r"C:\\cache", False))
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
                cleanup._list_purge_entries(r"C:\\cache\\nested", r"C:\\cache")


        def test_already_pinned_root_does_not_open_second_handle(
            monkeypatch: pytest.MonkeyPatch,
        ) -> None:
            monkeypatch.setattr(
                cleanup,
                "_open_exact_directory",
                lambda _path: (_ for _ in ()).throw(
                    AssertionError("selected root is already pinned")
                ),
            )
            class EmptyScan:
                def __enter__(self) -> EmptyScan:
                    return self
                def __exit__(self, *_args: object) -> None:
                    return None
                def __iter__(self) -> object:
                    return iter(())
            monkeypatch.setattr(os, "scandir", lambda _path: EmptyScan())

            assert cleanup._list_purge_entries(
                r"C:\\cache", r"C:\\approved", already_pinned=True
            ) == ()


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
                lambda _handle, _boundary, *, allow_equal: calls.append(
                    f"boundary:{allow_equal}"
                ),
            )
            monkeypatch.setattr(
                cleanup, "_require_final_path", lambda _handle, _path: calls.append("final")
            )
            monkeypatch.setattr(
                cleanup, "_set_delete_disposition", lambda _handle, _path: calls.append("delete")
            )
            monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)

            cleanup._delete_empty_directory(r"C:\\cache\\nested", r"C:\\cache")

            assert calls == ["boundary:False", "final", "delete"]
        '''
    ).lstrip(),
    encoding="utf-8",
)

Path("docs/execution-safety-reaudit.md").write_text(
    dedent(
        '''
        # Execution safety second-pass re-audit

        ## Scope

        This pass re-audits the shared Windows exact-mutation boundary after the rule-authority phases: handle identity, reparse/junction confinement, hard-link preservation, and concurrent rename/replacement behavior.

        ## Finding

        The selected root was pinned and observed reparse entries were deleted as links, but a nested ordinary directory could be replaced by a junction after pathname classification and before later traversal. A subsequent pathname `scandir()` could therefore resolve outside the approved tree before a child handle was checked.

        ## Correction

        The already-pinned selected root is reused. Every nested directory is opened with `OPEN_REPARSE_POINT` immediately before traversal, must remain ordinary/non-Cloud/in-boundary at its expected final path, every leaf handle is checked against the approved root before delete disposition, and nested directories are revalidated again before their own deletion. Existing exact-file snapshot, link-count, share-mode, root pinning, and postcondition guards are retained.

        ## Acceptance gate

        Merge only from the exact final head after lock/dependency checks, Ruff, strict mypy, full pytest/current CI, Windows EXE artifact, and CodeQL are green.
        '''
    ).lstrip(),
    encoding="utf-8",
)

tracker = Path("docs/full-rule-reaudit-2026-08.md")
tracker_text = tracker.read_text(encoding="utf-8")
old = "| Execution identity/reparse/hardlink/concurrency gates | ⏳ queued | second-pass regression audit; no weakening planned |"
new = "| Execution identity/reparse/hardlink/concurrency gates | ✅ phase 7 | nested junction replacement TOCTOU closed with per-directory/per-leaf handle confinement; existing snapshot/link/share guards retained |"
if old not in tracker_text:
    raise SystemExit("tracker execution-safety row anchor missing")
tracker.write_text(tracker_text.replace(old, new, 1), encoding="utf-8")
