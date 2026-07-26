"""Handle-bound Windows mutations for an already approved ordinary object.

This module is deliberately narrow.  It cannot discover objects, classify scan
results, or widen an approved root.  Every operation opens the final object
with ``OPEN_REPARSE_POINT``, compares metadata from that exact handle with the
scan snapshot, and mutates that handle only.

Directory support follows the same rule rather than relaxing it.  A whole-tree
purge keeps the verified root handle open for the entire traversal, preventing
the selected directory object from being renamed or replaced.  The walk never
traverses a reparse point: a link is removed as a link, so no descent can leave
the selected tree.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final, cast

from devclean.platform.windows.filesystem import (
    FileSystemMetadata,
    read_file_metadata,
    read_file_metadata_handle,
)

_DELETE: Final = 0x00010000
_FILE_READ_ATTRIBUTES: Final = 0x0080
_FILE_LIST_DIRECTORY: Final = 0x0001
_SYNCHRONIZE: Final = 0x00100000
_FILE_SHARE_READ: Final = 0x00000001
_FILE_SHARE_WRITE: Final = 0x00000002
_FILE_SHARE_DELETE: Final = 0x00000004
_MUTATION_SHARE_MODE: Final = _FILE_SHARE_READ
_CHILD_CREATE_SHARE_MODE: Final = _FILE_SHARE_READ | _FILE_SHARE_WRITE
_OPEN_EXISTING: Final = 3
_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_FLAG_OPEN_NO_RECALL: Final = 0x00100000
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_FILE_DISPOSITION_INFO_CLASS: Final = 4
_FILE_DISPOSITION_INFO_EX_CLASS: Final = 21
_FILE_DISPOSITION_FLAG_DELETE: Final = 0x00000001
_FILE_DISPOSITION_FLAG_POSIX_SEMANTICS: Final = 0x00000002
_FO_DELETE: Final = 0x0003
_FOF_SILENT: Final = 0x0004
_FOF_NOCONFIRMATION: Final = 0x0010
_FOF_ALLOWUNDO: Final = 0x0040
_FOF_NOERRORUI: Final = 0x0400
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
_ERROR_INVALID_PARAMETER: Final = 87
_ERROR_NOT_SUPPORTED: Final = 50
_FILE_ATTRIBUTE_DIRECTORY: Final = 0x00000010
_FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400


class ExactCleanupError(RuntimeError):
    """An exact-object mutation was refused or could not be verified."""


@dataclass(frozen=True, slots=True)
class ExactFileSnapshot:
    """The stable fields captured by a completed scan."""

    logical_size: int
    volume_serial: int
    file_id: str
    file_id_kind: str
    link_count: int
    attributes: int | None
    reparse_tag: int | None
    creation_time_ns: int
    last_write_time_ns: int


@dataclass(frozen=True, slots=True)
class ExactDirectorySnapshot:
    """The stable identity of a directory captured by a completed scan.

    Size and last-write time are deliberately absent.  A directory's mtime
    changes whenever any child is added or removed, so requiring it to match
    would refuse every actively used cache without adding any identity strength:
    the volume serial and 128-bit file id already pin exactly one object, and
    the caller additionally re-verifies the opened handle's final path.
    """

    volume_serial: int
    file_id: str
    file_id_kind: str
    creation_time_ns: int


@dataclass(frozen=True, slots=True)
class DirectoryPurgeProgress:
    """Incremental counters reported while a selected tree is removed."""

    files_removed: int
    links_removed: int
    directories_removed: int
    bytes_removed: int


@dataclass(frozen=True, slots=True)
class DirectoryPurgeResult:
    """Postcondition evidence for one selected-tree removal."""

    root_path: str
    files_removed: int
    links_removed: int
    directories_removed: int
    bytes_removed: int
    root_absent: bool
    completed: bool


@dataclass(frozen=True, slots=True)
class ExactMutationResult:
    """Postcondition evidence for one handle-bound mutation."""

    source_path: str
    destination_path: str | None
    source_name_absent: bool
    source_name_replaced: bool
    destination_matches: bool
    # Only recycling sets this.  ``False`` after a recycle means the item did not
    # reach the bin -- Windows deletes outright when an item does not fit -- so
    # the caller must not report it as recoverable.
    recycled: bool = False


class _SHFILEOPSTRUCTW(ctypes.Structure):
    """``SHFILEOPSTRUCTW`` as documented; ``fFlags`` really is a WORD."""

    _fields_ = (
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", ctypes.c_wchar_p),
        ("pTo", ctypes.c_wchar_p),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", ctypes.c_void_p),
        ("lpszProgressTitle", ctypes.c_wchar_p),
    )


class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD),
        ("i64Size", ctypes.c_longlong),
        ("i64NumItems", ctypes.c_longlong),
    )


@dataclass(frozen=True, slots=True)
class ExactRootBoundary:
    """Stable identity of the user-approved root that bounds one mutation."""

    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    # Win32 declares DeleteFile as BOOLEAN (one byte), not BOOL (four bytes).
    _fields_ = [("delete_file", ctypes.c_ubyte)]


class _FILE_DISPOSITION_INFO_EX(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD)]


def purge_exact_file(
    source: Path,
    expected: ExactFileSnapshot,
    boundary: ExactRootBoundary,
) -> ExactMutationResult:
    """Permanently delete the exact opened object after snapshot verification.

    No pathname-based ``DeleteFile`` call and no recursive API is used.  The
    disposition is attached to the verified handle, so a concurrent rename can
    change the visible name but cannot substitute a different object for the
    one being purged.
    """

    source_path = _ordinary_absolute_path(source, "source")
    root_handle, root_final = _open_boundary(boundary)
    try:
        handle = _open_exact_file(source_path)
        try:
            _require_snapshot(read_file_metadata_handle(handle), expected)
            _require_handle_in_boundary(handle, root_final, allow_equal=False)
            _set_delete_disposition(handle, source_path)
        finally:
            _close_handle(handle)
    finally:
        _close_handle(root_handle)
    absent, replaced = _source_name_state(source_path, expected)
    if not absent and not replaced:
        raise ExactCleanupError("verified object still exists after permanent purge")
    return ExactMutationResult(
        source_path=source_path,
        destination_path=None,
        source_name_absent=absent,
        source_name_replaced=replaced,
        destination_matches=False,
    )


def recycle_exact_object(
    source: Path,
    expected: ExactFileSnapshot,
    boundary: ExactRootBoundary,
) -> ExactMutationResult:
    """Send the verified object to the Windows Recycle Bin.

    Identity is checked on a handle first, exactly as the permanent path does.
    The Shell then takes a pathname, because that is the only interface the
    Recycle Bin has -- there is no handle-based recycle -- so a rename between
    the check and the call could in principle move a different object.  The
    outcome of that race is a recoverable item in the bin rather than a
    destroyed one, and the alternative was a private staging directory that
    never frees any space, so the pathname call is the right trade here.

    Windows silently deletes outright when an item does not fit the volume's
    bin, which is why this verifies afterwards that the bin actually grew.  A
    caller that gets ``recycled=False`` knows the object is gone for good.
    """

    source_path = _ordinary_absolute_path(source, "source")
    is_directory = bool(
        expected.attributes is not None
        and expected.attributes & _FILE_ATTRIBUTE_DIRECTORY
    )
    root_handle, root_final = _open_boundary(boundary)
    try:
        handle = (
            _open_exact_directory_for_mutation(source_path)
            if is_directory
            else _open_exact_file(source_path)
        )
        try:
            metadata = read_file_metadata_handle(handle)
            if is_directory:
                _require_directory_snapshot(
                    metadata,
                    _directory_snapshot_from_file_snapshot(expected),
                )
                _require_final_path(handle, source_path)
            else:
                _require_snapshot(metadata, expected)
            _require_handle_in_boundary(handle, root_final, allow_equal=False)
        finally:
            _close_handle(handle)
    finally:
        _close_handle(root_handle)

    before = _recycle_bin_item_count(source_path)
    _shell_delete_to_recycle_bin(source_path)
    absent, replaced = (
        _directory_source_name_state(
            source_path,
            _directory_snapshot_from_file_snapshot(expected),
        )
        if is_directory
        else _source_name_state(source_path, expected)
    )
    if not absent and not replaced:
        raise ExactCleanupError("verified object still exists after recycling")
    after = _recycle_bin_item_count(source_path)
    recycled = before is not None and after is not None and after > before
    return ExactMutationResult(
        source_path=source_path,
        destination_path=None,
        source_name_absent=absent,
        source_name_replaced=replaced,
        destination_matches=False,
        recycled=recycled,
    )


def _shell_delete_to_recycle_bin(path: str) -> None:
    """Delete one path through the Shell, requesting the Recycle Bin.

    ``SHFileOperationW`` is used rather than ``IFileOperation`` because it is one
    documented call with one status code, and the extended-length prefix is
    deliberately absent: Shell APIs reject it.
    """

    if os.name != "nt":
        raise ExactCleanupError("recycling requires Windows")
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    operation = _SHFILEOPSTRUCTW()
    operation.hwnd = None
    operation.wFunc = _FO_DELETE
    # The Shell expects a double-null-terminated list even for one entry.
    operation.pFrom = ctypes.c_wchar_p(path + "\0\0")
    operation.pTo = None
    operation.fFlags = (
        _FOF_ALLOWUNDO | _FOF_NOCONFIRMATION | _FOF_SILENT | _FOF_NOERRORUI
    )
    operation.fAnyOperationsAborted = 0
    operation.hNameMappings = None
    operation.lpszProgressTitle = None
    shell_operation = shell32.SHFileOperationW
    shell_operation.argtypes = (ctypes.POINTER(_SHFILEOPSTRUCTW),)
    shell_operation.restype = ctypes.c_int
    status = shell_operation(ctypes.byref(operation))
    if status != 0:
        raise ExactCleanupError(f"Shell delete failed with status {status} for {path}")
    if operation.fAnyOperationsAborted:
        raise ExactCleanupError(f"Shell delete was aborted for {path}")


def _recycle_bin_item_count(path: str) -> int | None:
    """Return the item count in the Recycle Bin for *path*'s volume, or None.

    The count is the evidence that a recycle really recycled.  ``None`` means the
    query itself failed, in which case the caller reports the outcome as
    unverified rather than claiming success.
    """

    if os.name != "nt":
        return None
    drive = os.path.splitdrive(path)[0]
    if not drive:
        return None
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    info = _SHQUERYRBINFO()
    info.cbSize = ctypes.sizeof(_SHQUERYRBINFO)
    query = shell32.SHQueryRecycleBinW
    query.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(_SHQUERYRBINFO))
    query.restype = ctypes.c_long
    if query(f"{drive}\\", ctypes.byref(info)) != 0:
        return None
    return int(info.i64NumItems)


def purge_exact_directory_tree(
    root: Path,
    expected: ExactDirectorySnapshot,
    boundary: ExactRootBoundary,
    *,
    on_progress: Callable[[DirectoryPurgeProgress], None] | None = None,
    is_cancelled: Callable[[], bool] | None = None,
) -> DirectoryPurgeResult:
    """Remove a selected tree bottom-up, one verified handle at a time.

    The verified root handle stays open until traversal and root deletion have
    finished, so the selected root object cannot be replaced by another
    directory at the same path.  Reparse points are removed as links and never
    descended into, so the walk cannot leave the selected tree.  A cancelled or
    failed run stops immediately and reports what it already removed; it is
    never resumed automatically.
    """

    root_path = _ordinary_absolute_path(root, "tree root")
    root_handle, root_final = _open_boundary(boundary)
    try:
        # Confinement is the approved root, enforced on the opened handle below.
        # The walk itself cannot widen it: it never descends a reparse point, so
        # the only paths it can reach are real children of this directory.
        handle = _open_exact_directory_for_mutation(root_path)
        try:
            _require_directory_snapshot(read_file_metadata_handle(handle), expected)
            _require_handle_in_boundary(handle, root_final, allow_equal=False)
            _require_final_path(handle, root_path)
            state = _TreePurgeState()
            completed = _purge_tree_contents(
                root_path, state, on_progress, is_cancelled
            )
            if completed:
                _set_delete_disposition(handle, root_path)
                state.directories_removed += 1
        finally:
            _close_handle(handle)
    finally:
        _close_handle(root_handle)
    return DirectoryPurgeResult(
        root_path=root_path,
        files_removed=state.files_removed,
        links_removed=state.links_removed,
        directories_removed=state.directories_removed,
        bytes_removed=state.bytes_removed,
        root_absent=not os.path.lexists(root_path),
        completed=completed,
    )


def directory_metadata_matches_snapshot(
    metadata: FileSystemMetadata | None, expected: ExactDirectorySnapshot
) -> bool:
    """Public read-only helper used by durable reconciliation."""

    return _directory_metadata_matches(metadata, expected)


def metadata_matches_snapshot(
    metadata: FileSystemMetadata | None, expected: ExactFileSnapshot
) -> bool:
    """Public read-only helper used by durable reconciliation."""

    return _metadata_matches(metadata, expected)


def _ordinary_absolute_path(path: Path, label: str) -> str:
    text = os.path.abspath(os.fspath(path))
    if (
        not os.path.isabs(text)
        or text.startswith(("\\\\?\\", "\\\\.\\", "\\\\", "//"))
        or "\x00" in text
    ):
        raise ExactCleanupError(f"{label} must be an ordinary absolute local path")
    return text


def _open_exact_file(path: str) -> wintypes.HANDLE:
    if os.name != "nt":
        raise ExactCleanupError("exact cleanup mutations require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _extended_path(path),
        _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        # Omitting WRITE and DELETE sharing fails closed if an existing writer
        # or pathname mutator is active and prevents either from being opened
        # after the final-path/snapshot validation.
        _MUTATION_SHARE_MODE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_OPEN_NO_RECALL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _raise_windows_error("open exact cleanup target", path)
    return cast(wintypes.HANDLE, handle)


def _open_exact_directory(
    path: str,
    *,
    share_mode: int = _CHILD_CREATE_SHARE_MODE,
) -> wintypes.HANDLE:
    """Pin a directory against replacement while allowing child mutations."""

    if os.name != "nt":
        raise ExactCleanupError("exact cleanup mutations require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        path,
        _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        share_mode,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS
        | _FILE_FLAG_OPEN_REPARSE_POINT
        | _FILE_FLAG_OPEN_NO_RECALL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _raise_windows_error("open pinned cleanup destination", path)
    return cast(wintypes.HANDLE, handle)


def _open_exact_directory_for_mutation(path: str) -> wintypes.HANDLE:
    """Pin a directory object itself for a verified rename or delete."""

    if os.name != "nt":
        raise ExactCleanupError("exact cleanup mutations require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _extended_path(path),
        _DELETE | _FILE_LIST_DIRECTORY | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        # As for files, omitting WRITE and DELETE sharing fails closed when
        # another process already holds the directory for renaming or deletion.
        _MUTATION_SHARE_MODE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS
        | _FILE_FLAG_OPEN_REPARSE_POINT
        | _FILE_FLAG_OPEN_NO_RECALL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _raise_windows_error("open exact cleanup directory", path)
    return cast(wintypes.HANDLE, handle)


def _extended_path(path: str) -> str:
    r"""Return the ``\\?\`` form of an already validated ordinary absolute path.

    ``_ordinary_absolute_path`` rejects extended-length input precisely so a
    caller cannot smuggle in a path that skips Win32 normalisation.  Once a path
    has passed that gate and been normalised, prefixing it here is what lets a
    selected tree deeper than ``MAX_PATH`` be removed at all; real
    package-manager caches frequently exceed the legacy limit.
    """

    if path.startswith("\\\\?\\"):
        raise ExactCleanupError("path was already extended before validation")
    return f"\\\\?\\{path}"


class _TreePurgeState:
    __slots__ = ("bytes_removed", "directories_removed", "files_removed", "links_removed")

    def __init__(self) -> None:
        self.files_removed = 0
        self.links_removed = 0
        self.directories_removed = 0
        self.bytes_removed = 0

    def snapshot(self) -> DirectoryPurgeProgress:
        return DirectoryPurgeProgress(
            files_removed=self.files_removed,
            links_removed=self.links_removed,
            directories_removed=self.directories_removed,
            bytes_removed=self.bytes_removed,
        )


def _purge_tree_contents(
    root_path: str,
    state: _TreePurgeState,
    on_progress: Callable[[DirectoryPurgeProgress], None] | None,
    is_cancelled: Callable[[], bool] | None,
) -> bool:
    """Empty *root_path* bottom-up.  Returns whether the walk ran to completion.

    The traversal keeps its own stack rather than recursing, because real caches
    reach hundreds of thousands of entries and Python's recursion limit is not a
    property this should depend on.
    """

    pending: list[tuple[str, bool]] = [(root_path, False)]
    while pending:
        if is_cancelled is not None and is_cancelled():
            return False
        current, children_done = pending.pop()
        if children_done:
            if current != root_path:
                _delete_empty_directory(current)
                state.directories_removed += 1
                if on_progress is not None:
                    on_progress(state.snapshot())
            continue
        children = _list_purge_entries(current)
        pending.append((current, True))
        for child_path, kind, size in children:
            if kind is _EntryKind.DIRECTORY:
                pending.append((child_path, False))
                continue
            # A reparse point is removed as a link.  Descending into one is the
            # single way a tree walk could escape its selected root, so the walk
            # never does it -- not even for a link that appears to stay inside.
            _delete_leaf(child_path)
            if kind is _EntryKind.REPARSE_POINT:
                state.links_removed += 1
            else:
                state.files_removed += 1
                state.bytes_removed += size
        if on_progress is not None:
            on_progress(state.snapshot())
    return True


class _EntryKind(Enum):
    FILE = "FILE"
    DIRECTORY = "DIRECTORY"
    REPARSE_POINT = "REPARSE_POINT"


def _list_purge_entries(directory: str) -> tuple[tuple[str, _EntryKind, int], ...]:
    entries: list[tuple[str, _EntryKind, int]] = []
    with os.scandir(_extended_path(directory)) as scan:
        for entry in scan:
            try:
                status = entry.stat(follow_symlinks=False)
            except OSError:
                # The entry vanished between enumeration and stat.  Nothing is
                # left to remove, so treat it as already gone rather than
                # failing the whole tree.
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


def _delete_leaf(path: str) -> None:
    """Delete one file, or one link, by handle without ever resolving it.

    A reparse point may itself be directory-shaped -- a junction or a directory
    symlink -- and Windows refuses to open any directory-shaped object without
    backup semantics.  Requesting them here is what lets the link be removed as
    a link instead of forcing the walk to follow it.
    """

    handle = _open_leaf_for_delete(path)
    try:
        _set_delete_disposition(handle, path)
    finally:
        _close_handle(handle)


def _open_leaf_for_delete(path: str) -> wintypes.HANDLE:
    if os.name != "nt":
        raise ExactCleanupError("exact cleanup mutations require Windows")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        _extended_path(path),
        _DELETE | _FILE_READ_ATTRIBUTES | _SYNCHRONIZE,
        _MUTATION_SHARE_MODE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS
        | _FILE_FLAG_OPEN_REPARSE_POINT
        | _FILE_FLAG_OPEN_NO_RECALL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _raise_windows_error("open selected tree entry", path)
    return cast(wintypes.HANDLE, handle)


def _delete_empty_directory(path: str) -> None:
    handle = _open_exact_directory_for_mutation(path)
    try:
        _set_delete_disposition(handle, path)
    finally:
        _close_handle(handle)


def _open_boundary(boundary: ExactRootBoundary) -> tuple[wintypes.HANDLE, str]:
    root_path = _ordinary_absolute_path(boundary.path, "approved root")
    handle = _open_exact_directory(
        root_path,
        # A directory handle must allow child-entry writes or the verified
        # rename cannot add/remove a child.  DELETE sharing remains omitted,
        # so the directory object itself cannot be replaced while pinned.
        share_mode=_CHILD_CREATE_SHARE_MODE,
    )
    try:
        metadata = read_file_metadata_handle(handle)
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or metadata.is_cloud_placeholder
            or metadata.volume_serial != boundary.volume_serial
            or metadata.file_id != boundary.file_id
            or metadata.file_id_kind != boundary.file_id_kind
        ):
            raise ExactCleanupError("approved root handle identity changed")
        return (handle, _final_path(handle))
    except Exception:
        _close_handle(handle)
        raise


def _close_handle(handle: wintypes.HANDLE) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL
    close(handle)


def _set_delete_disposition(handle: wintypes.HANDLE, source: str) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    extended = _FILE_DISPOSITION_INFO_EX(
        _FILE_DISPOSITION_FLAG_DELETE | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
    )
    if set_information(
        handle,
        _FILE_DISPOSITION_INFO_EX_CLASS,
        ctypes.byref(extended),
        ctypes.sizeof(extended),
    ):
        return
    error = ctypes.get_last_error()
    if error not in {_ERROR_INVALID_PARAMETER, _ERROR_NOT_SUPPORTED}:
        raise OSError(error, ctypes.FormatError(error), source)
    basic = _FILE_DISPOSITION_INFO(True)
    if not set_information(
        handle,
        _FILE_DISPOSITION_INFO_CLASS,
        ctypes.byref(basic),
        ctypes.sizeof(basic),
    ):
        _raise_windows_error("set verified cleanup disposition", source)


def _final_path(handle: wintypes.HANDLE) -> str:
    """Return the normalized resolved DOS path for an already-open handle."""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = (
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    )
    get_final.restype = wintypes.DWORD
    required = int(get_final(handle, None, 0, 0))
    if required == 0 or required > 32_767:
        _raise_windows_error("resolve opened cleanup handle", "opened handle")
    buffer = ctypes.create_unicode_buffer(required + 1)
    written = int(get_final(handle, buffer, len(buffer), 0))
    if written == 0 or written >= len(buffer):
        _raise_windows_error("resolve opened cleanup handle", "opened handle")
    path = buffer.value
    if path.startswith("\\\\?\\UNC\\"):
        path = "\\\\" + path[8:]
    elif path.startswith("\\\\?\\"):
        path = path[4:]
    return os.path.normcase(os.path.normpath(path))


def _require_handle_in_boundary(
    handle: wintypes.HANDLE,
    root_final: str,
    *,
    allow_equal: bool,
) -> None:
    current = _final_path(handle)
    try:
        common = os.path.commonpath((current, root_final))
    except ValueError as error:
        raise ExactCleanupError("opened object escaped its approved root volume") from error
    if common != root_final or (not allow_equal and current == root_final):
        raise ExactCleanupError("opened object's final path escaped the approved root")


def _require_snapshot(metadata: FileSystemMetadata, expected: ExactFileSnapshot) -> None:
    if not _metadata_matches(metadata, expected):
        raise ExactCleanupError("file identity or metadata changed since the completed scan")


def _require_directory_snapshot(
    metadata: FileSystemMetadata, expected: ExactDirectorySnapshot
) -> None:
    if not _directory_metadata_matches(metadata, expected):
        raise ExactCleanupError("directory identity changed since the completed scan")


def _directory_metadata_matches(
    metadata: FileSystemMetadata | None, expected: ExactDirectorySnapshot
) -> bool:
    if metadata is None:
        return False
    return (
        metadata.is_directory
        and not metadata.is_reparse_point
        and not metadata.is_cloud_placeholder
        and metadata.volume_serial == expected.volume_serial
        and metadata.file_id == expected.file_id
        and metadata.file_id_kind == expected.file_id_kind
        and metadata.creation_time_ns == expected.creation_time_ns
    )


def _directory_snapshot_from_file_snapshot(
    expected: ExactFileSnapshot,
) -> ExactDirectorySnapshot:
    return ExactDirectorySnapshot(
        volume_serial=expected.volume_serial,
        file_id=expected.file_id,
        file_id_kind=expected.file_id_kind,
        creation_time_ns=expected.creation_time_ns,
    )


def _directory_source_name_state(
    path: str, expected: ExactDirectorySnapshot
) -> tuple[bool, bool]:
    metadata = _read_optional_metadata(path)
    if metadata is None:
        return (True, False)
    return (False, not _directory_metadata_matches(metadata, expected))


def _require_final_path(handle: wintypes.HANDLE, expected_path: str) -> None:
    """Refuse when the opened object no longer answers to the recorded name.

    Identity alone cannot detect that a directory was renamed between the scan
    and the confirmation the user typed.  Comparing the handle's resolved path
    with the path shown in the plan closes that gap.
    """

    actual = _final_path(handle)
    if actual != os.path.normcase(os.path.normpath(expected_path)):
        raise ExactCleanupError("opened object's final path no longer matches the plan")


def _metadata_matches(
    metadata: FileSystemMetadata | None, expected: ExactFileSnapshot
) -> bool:
    if metadata is None:
        return False
    return (
        not metadata.is_directory
        and not metadata.is_reparse_point
        and not metadata.is_cloud_placeholder
        and metadata.logical_size == expected.logical_size
        and metadata.volume_serial == expected.volume_serial
        and metadata.file_id == expected.file_id
        and metadata.file_id_kind == expected.file_id_kind
        and metadata.link_count == expected.link_count == 1
        and metadata.attributes == expected.attributes
        and metadata.reparse_tag == expected.reparse_tag
        and metadata.creation_time_ns == expected.creation_time_ns
        and metadata.last_write_time_ns == expected.last_write_time_ns
    )


def _source_name_state(path: str, expected: ExactFileSnapshot) -> tuple[bool, bool]:
    metadata = _read_optional_metadata(path)
    if metadata is None:
        return (True, False)
    return (False, not _metadata_matches(metadata, expected))


def _read_optional_metadata(path: str) -> FileSystemMetadata | None:
    try:
        return read_file_metadata(path)
    except FileNotFoundError:
        return None
    except OSError as error:
        if getattr(error, "winerror", None) in {2, 3} or error.errno == 2:
            return None
        raise


def _raise_windows_error(operation: str, path: str) -> None:
    error = ctypes.get_last_error()
    raise OSError(error, f"{operation}: {ctypes.FormatError(error)}", path)


__all__ = [
    "DirectoryPurgeProgress",
    "DirectoryPurgeResult",
    "ExactCleanupError",
    "ExactDirectorySnapshot",
    "ExactFileSnapshot",
    "ExactMutationResult",
    "ExactRootBoundary",
    "directory_metadata_matches_snapshot",
    "metadata_matches_snapshot",
    "purge_exact_directory_tree",
    "purge_exact_file",
    "recycle_exact_object",
]
