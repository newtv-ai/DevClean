from __future__ import annotations

from pathlib import Path


path = Path("src/devclean/platform/windows/exact_cleanup.py")
text = path.read_text(encoding="utf-8")

replacements = [
    (
        """            completed = _purge_tree_contents(\n                root_path, state, on_progress, is_cancelled\n            )\n""",
        """            completed = _purge_tree_contents(\n                root_path, root_final, state, on_progress, is_cancelled\n            )\n""",
    ),
    (
        """def _purge_tree_contents(\n    root_path: str,\n    state: _TreePurgeState,\n""",
        """def _purge_tree_contents(\n    root_path: str,\n    boundary_final: str,\n    state: _TreePurgeState,\n""",
    ),
    ("                _delete_empty_directory(current)\n", "                _delete_empty_directory(current, boundary_final)\n"),
    ("        children = _list_purge_entries(current)\n", "        children = _list_purge_entries(current, boundary_final)\n"),
    ("            _delete_leaf(child_path)\n", "            _delete_leaf(child_path, boundary_final)\n"),
    (
        """def _list_purge_entries(directory: str) -> tuple[tuple[str, _EntryKind, int], ...]:\n    entries: list[tuple[str, _EntryKind, int]] = []\n    with os.scandir(_extended_path(directory)) as scan:\n        for entry in scan:\n            try:\n                status = entry.stat(follow_symlinks=False)\n            except OSError:\n                # The entry vanished between enumeration and stat.  Nothing is\n                # left to remove, so treat it as already gone rather than\n                # failing the whole tree.\n                continue\n            child = os.path.join(directory, entry.name)\n            attributes = getattr(status, \"st_file_attributes\", 0)\n            if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:\n                entries.append((child, _EntryKind.REPARSE_POINT, 0))\n            elif attributes & _FILE_ATTRIBUTE_DIRECTORY:\n                entries.append((child, _EntryKind.DIRECTORY, 0))\n            else:\n                entries.append((child, _EntryKind.FILE, status.st_size))\n    return tuple(entries)\n""",
        """def _list_purge_entries(\n    directory: str, boundary_final: str\n) -> tuple[tuple[str, _EntryKind, int], ...]:\n    # Pin every directory at the moment it is traversed. The root handle alone\n    # cannot stop a nested ordinary directory from being replaced by a junction\n    # after a prior enumeration. OPEN_REPARSE_POINT plus the final-path and\n    # approved-root checks turn that race into a refusal before scandir follows\n    # an ancestor outside the selected tree.\n    handle = _open_exact_directory(directory)\n    try:\n        metadata = read_file_metadata_handle(handle)\n        if (\n            not metadata.is_directory\n            or metadata.is_reparse_point\n            or metadata.is_cloud_placeholder\n        ):\n            raise ExactCleanupError(\"selected tree directory became a reparse/cloud boundary\")\n        _require_handle_in_boundary(handle, boundary_final, allow_equal=False)\n        _require_final_path(handle, directory)\n        entries: list[tuple[str, _EntryKind, int]] = []\n        with os.scandir(_extended_path(directory)) as scan:\n            for entry in scan:\n                try:\n                    status = entry.stat(follow_symlinks=False)\n                except OSError:\n                    # The entry vanished between enumeration and stat. Nothing is\n                    # left to remove, so treat it as already gone rather than\n                    # failing the whole tree.\n                    continue\n                child = os.path.join(directory, entry.name)\n                attributes = getattr(status, \"st_file_attributes\", 0)\n                if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:\n                    entries.append((child, _EntryKind.REPARSE_POINT, 0))\n                elif attributes & _FILE_ATTRIBUTE_DIRECTORY:\n                    entries.append((child, _EntryKind.DIRECTORY, 0))\n                else:\n                    entries.append((child, _EntryKind.FILE, status.st_size))\n        return tuple(entries)\n    finally:\n        _close_handle(handle)\n""",
    ),
    ("def _delete_leaf(path: str) -> None:\n", "def _delete_leaf(path: str, boundary_final: str) -> None:\n"),
    (
        """    handle = _open_leaf_for_delete(path)\n    try:\n        _set_delete_disposition(handle, path)\n""",
        """    handle = _open_leaf_for_delete(path)\n    try:\n        _require_handle_in_boundary(handle, boundary_final, allow_equal=False)\n        _set_delete_disposition(handle, path)\n""",
    ),
    (
        """def _delete_empty_directory(path: str) -> None:\n    handle = _open_exact_directory_for_mutation(path)\n    try:\n        _set_delete_disposition(handle, path)\n""",
        """def _delete_empty_directory(path: str, boundary_final: str) -> None:\n    handle = _open_exact_directory_for_mutation(path)\n    try:\n        metadata = read_file_metadata_handle(handle)\n        if (\n            not metadata.is_directory\n            or metadata.is_reparse_point\n            or metadata.is_cloud_placeholder\n        ):\n            raise ExactCleanupError(\"selected tree directory changed before deletion\")\n        _require_handle_in_boundary(handle, boundary_final, allow_equal=False)\n        _require_final_path(handle, path)\n        _set_delete_disposition(handle, path)\n""",
    ),
]

for old, new in replacements:
    if old not in text:
        raise SystemExit(f"expected exact_cleanup patch anchor missing: {old[:100]!r}")
    text = text.replace(old, new, 1)
path.write_text(text, encoding="utf-8")

test = Path("tests/test_exact_cleanup_safety.py")
test.write_text(
    '''from __future__ import annotations

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
        lambda actual, path: calls.append(("delete", (actual, path))),
    )
    monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)

    cleanup._delete_leaf(r"C:\\cache\\x.bin", r"C:\\cache")

    assert calls[0] == ("boundary", (handle, r"C:\\cache", False))
    assert calls[1][0] == "delete"


def test_nested_reparse_is_refused_before_scandir(monkeypatch: pytest.MonkeyPatch) -> None:
    handle = object()
    monkeypatch.setattr(cleanup, "_open_exact_directory", lambda _path: handle)
    monkeypatch.setattr(
        cleanup,
        "read_file_metadata_handle",
        lambda _handle: _metadata(directory=True, reparse=True),
    )
    monkeypatch.setattr(cleanup, "_close_handle", lambda _handle: None)
    monkeypatch.setattr(
        cleanup.os,
        "scandir",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("scandir must not follow replaced junction")
        ),
    )

    with pytest.raises(ExactCleanupError, match="reparse/cloud"):
        cleanup._list_purge_entries(r"C:\\cache\\nested", r"C:\\cache")


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

    cleanup._delete_empty_directory(r"C:\\cache\\nested", r"C:\\cache")

    assert calls == ["boundary:False", "final", "delete"]
''',
    encoding="utf-8",
)

Path("docs/execution-safety-reaudit.md").write_text(
    '''# Execution safety second-pass re-audit

## Scope

This pass re-audits the shared Windows exact-mutation boundary after the rule-authority phases. It covers handle identity, reparse/junction confinement, hard-link preservation, and concurrent rename/replacement behavior.

## Finding

The selected root directory was pinned for a whole-tree purge, and reparse entries observed during enumeration were deleted as links. However, a nested directory was classified by pathname and then revisited later by pathname. A concurrent actor could replace that nested directory with a junction between those two moments. The subsequent `os.scandir()` would resolve the ancestor junction before DevClean opened any child handle, so the mutation layer could enumerate outside the approved tree and later attempt a child deletion there.

## Correction

- every directory is opened with `OPEN_REPARSE_POINT` immediately before traversal;
- the opened directory must still be ordinary, non-Cloud, within the pinned approved root, and have the expected final pathname before `scandir`;
- every leaf handle is independently checked against the approved-root final path before delete disposition;
- every nested directory is revalidated as ordinary, in-boundary, and at the expected final path before its own delete disposition;
- a junction/reparse substitution now fails closed before traversal can follow it;
- existing exact-file snapshot checks, link-count equality, non-delete sharing, root pinning, and postconditions remain unchanged.

## Hard-link and concurrency conclusion

Exact file cleanup already compares current handle metadata with the scan snapshot, including link count, and destructive file opens omit delete/write sharing. Private DevClean state files separately reject multi-link files. This pass does not weaken those invariants.

Directory contents are allowed to change inside the approved cache root because whole-tree cleanup semantically removes that selected namespace. The security boundary is that pathname races must not redirect mutation outside that root; the new per-handle confinement checks enforce that invariant at each destructive step.

## Acceptance gate

Merge only from the exact final head after lock/dependency checks, Ruff, strict mypy, full pytest/current CI, Windows EXE artifact, and CodeQL are green.
''',
    encoding="utf-8",
)

tracker = Path("docs/full-rule-reaudit-2026-08.md")
tracker_text = tracker.read_text(encoding="utf-8")
old = "| Execution identity/reparse/hardlink/concurrency gates | ⏳ queued | second-pass regression audit; no weakening planned |"
new = "| Execution identity/reparse/hardlink/concurrency gates | ✅ phase 7 | per-directory and per-leaf handle confinement closes nested junction replacement TOCTOU; existing snapshot/link/share guards retained |"
if old not in tracker_text:
    raise SystemExit("tracker execution-safety row anchor missing")
tracker.write_text(tracker_text.replace(old, new, 1), encoding="utf-8")
