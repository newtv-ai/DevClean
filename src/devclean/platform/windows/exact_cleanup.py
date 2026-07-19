"""Handle-bound Windows mutations for an already approved ordinary file.

This module is deliberately narrow.  It cannot discover files, classify scan
results, widen an approved root, or recursively remove a directory.  Both
operations open the final object with ``OPEN_REPARSE_POINT``, compare metadata
from that exact handle with the scan snapshot, and mutate that handle only.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from devclean.platform.windows.filesystem import (
    FileSystemMetadata,
    read_file_metadata,
    read_file_metadata_handle,
)
from devclean.platform.windows.security import (
    audit_private_directory,
    create_private_directory,
)

QUARANTINE_DIRECTORY_NAME = ".DevClean-quarantine-v1"

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
_FILE_RENAME_INFO_CLASS: Final = 3
_FILE_DISPOSITION_INFO_CLASS: Final = 4
_FILE_DISPOSITION_INFO_EX_CLASS: Final = 21
_FILE_DISPOSITION_FLAG_DELETE: Final = 0x00000001
_FILE_DISPOSITION_FLAG_POSIX_SEMANTICS: Final = 0x00000002
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value
_ERROR_INVALID_PARAMETER: Final = 87
_ERROR_NOT_SUPPORTED: Final = 50


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
class ExactMutationResult:
    """Postcondition evidence for one handle-bound mutation."""

    source_path: str
    destination_path: str | None
    source_name_absent: bool
    source_name_replaced: bool
    destination_matches: bool


@dataclass(frozen=True, slots=True)
class ExactRootBoundary:
    """Stable identity of the user-approved root that bounds one mutation."""

    path: Path
    volume_serial: int
    file_id: str
    file_id_kind: str


class _FILE_RENAME_INFO_LAYOUT(ctypes.Structure):
    _fields_ = [
        ("replace_if_exists", wintypes.BOOL),
        ("root_directory", wintypes.HANDLE),
        ("file_name_length", wintypes.DWORD),
        ("file_name", wintypes.WCHAR * 1),
    ]


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    # Win32 declares DeleteFile as BOOLEAN (one byte), not BOOL (four bytes).
    _fields_ = [("delete_file", ctypes.c_ubyte)]


class _FILE_DISPOSITION_INFO_EX(ctypes.Structure):
    _fields_ = [("flags", wintypes.DWORD)]


def prepare_private_quarantine_directory(
    directory: Path,
    boundary: ExactRootBoundary,
) -> None:
    """Atomically create and pin one new batch quarantine directory.

    The directory must be a direct child of the already approved root.  The
    root handle stays open without write/delete sharing while the directory is
    created with its final private DACL.  Existing paths are never adopted.
    """

    directory_path = _ordinary_absolute_path(directory, "quarantine directory")
    _require_quarantine_child(directory_path, boundary)
    root_handle, root_final = _open_boundary(boundary)
    try:
        if os.path.lexists(directory_path):
            raise ExactCleanupError("quarantine directory already exists; refusing takeover")
        create_private_directory(Path(directory_path))
        _verify_open_quarantine_directory(directory_path, root_final)
    finally:
        _close_handle(root_handle)


def verify_private_quarantine_directory(
    directory: Path,
    boundary: ExactRootBoundary,
) -> None:
    """Re-pin a directory created earlier in this in-memory execution only."""

    directory_path = _ordinary_absolute_path(directory, "quarantine directory")
    _require_quarantine_child(directory_path, boundary)
    root_handle, root_final = _open_boundary(boundary)
    try:
        _verify_open_quarantine_directory(directory_path, root_final)
    finally:
        _close_handle(root_handle)


def quarantine_exact_file(
    source: Path,
    destination: Path,
    expected: ExactFileSnapshot,
    boundary: ExactRootBoundary,
) -> ExactMutationResult:
    """Rename the exact scanned file into a same-volume private staging path."""

    source_path = _ordinary_absolute_path(source, "source")
    destination_path = _ordinary_absolute_path(destination, "destination")
    if os.path.normcase(source_path) == os.path.normcase(destination_path):
        raise ExactCleanupError("source and quarantine destination must differ")
    if Path(source_path).anchor.casefold() != Path(destination_path).anchor.casefold():
        raise ExactCleanupError("quarantine destination must be on the source volume")
    if os.path.lexists(destination_path):
        raise ExactCleanupError("quarantine destination already exists")

    root_handle, root_final = _open_boundary(boundary)
    try:
        directory_handle = _open_exact_directory(str(Path(destination_path).parent))
        try:
            directory_before = read_file_metadata_handle(directory_handle)
            _require_destination_directory(directory_before, expected)
            _require_handle_in_boundary(directory_handle, root_final, allow_equal=True)
            handle = _open_exact_file(source_path)
            try:
                _require_snapshot(read_file_metadata_handle(handle), expected)
                _require_handle_in_boundary(handle, root_final, allow_equal=False)
                _rename_open_handle(handle, destination_path)
                _require_snapshot(read_file_metadata_handle(handle), expected)
                _require_handle_in_boundary(handle, root_final, allow_equal=False)
                if (
                    read_file_metadata_handle(directory_handle).identity
                    != directory_before.identity
                ):
                    raise ExactCleanupError("quarantine directory identity changed during rename")
            finally:
                _close_handle(handle)
        finally:
            _close_handle(directory_handle)
    finally:
        _close_handle(root_handle)

    destination_metadata = _read_optional_metadata(destination_path)
    destination_matches = _metadata_matches(destination_metadata, expected)
    if not destination_matches:
        raise ExactCleanupError("quarantine destination does not contain the verified object")
    absent, replaced = _source_name_state(source_path, expected)
    if not absent and not replaced:
        raise ExactCleanupError("source name still references the quarantined object")
    return ExactMutationResult(
        source_path=source_path,
        destination_path=destination_path,
        source_name_absent=absent,
        source_name_replaced=replaced,
        destination_matches=True,
    )


def restore_exact_file(
    quarantine_path: Path,
    original_path: Path,
    expected: ExactFileSnapshot,
    boundary: ExactRootBoundary,
) -> ExactMutationResult:
    """Restore an exact quarantined file when the original name is still free."""

    quarantine = _ordinary_absolute_path(quarantine_path, "quarantine source")
    original = _ordinary_absolute_path(original_path, "restore destination")
    if Path(quarantine).anchor.casefold() != Path(original).anchor.casefold():
        raise ExactCleanupError("restore destination must be on the quarantine volume")
    if os.path.lexists(original):
        raise ExactCleanupError("original path is occupied; refusing to replace it")
    root_handle, root_final = _open_boundary(boundary)
    try:
        directory_handle = _open_exact_directory(str(Path(original).parent))
        try:
            directory_before = read_file_metadata_handle(directory_handle)
            _require_destination_directory(directory_before, expected)
            _require_handle_in_boundary(directory_handle, root_final, allow_equal=True)
            handle = _open_exact_file(quarantine)
            try:
                _require_snapshot(read_file_metadata_handle(handle), expected)
                _require_handle_in_boundary(handle, root_final, allow_equal=False)
                _rename_open_handle(handle, original)
                _require_snapshot(read_file_metadata_handle(handle), expected)
                _require_handle_in_boundary(handle, root_final, allow_equal=False)
                if (
                    read_file_metadata_handle(directory_handle).identity
                    != directory_before.identity
                ):
                    raise ExactCleanupError("restore directory identity changed during rename")
            finally:
                _close_handle(handle)
        finally:
            _close_handle(directory_handle)
    finally:
        _close_handle(root_handle)
    restored = _read_optional_metadata(original)
    if not _metadata_matches(restored, expected):
        raise ExactCleanupError("restored name does not contain the verified object")
    absent, replaced = _source_name_state(quarantine, expected)
    return ExactMutationResult(
        source_path=quarantine,
        destination_path=original,
        source_name_absent=absent,
        source_name_replaced=replaced,
        destination_matches=True,
    )


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
        path,
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


def _rename_open_handle(handle: wintypes.HANDLE, destination: str) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    )
    set_information.restype = wintypes.BOOL
    destination = _ordinary_absolute_path(Path(destination), "rename destination")
    name = destination.encode("utf-16-le")
    # ``FILE_RENAME_INFO.FileName`` starts before the naturally padded
    # ``sizeof(header)`` on 64-bit Windows (commonly offset 20 vs size 24).
    # Use the actual field offset or the path would begin with padding NULs.
    name_offset = _FILE_RENAME_INFO_LAYOUT.file_name.offset
    # Although FileNameLength excludes the terminator, Windows file-system
    # drivers are not consistent about avoiding the following WCHAR.  Keep a
    # zero terminator inside the supplied buffer to prevent an out-of-bounds
    # suffix from becoming part of the renamed file name.
    buffer = ctypes.create_string_buffer(
        name_offset + len(name) + ctypes.sizeof(wintypes.WCHAR)
    )
    header = _FILE_RENAME_INFO_LAYOUT.from_buffer(buffer)
    header.replace_if_exists = False
    # The Win32 FILE_RENAME_INFO contract explicitly requires RootDirectory to
    # be NULL.  We still keep a non-delete-sharing handle to the already
    # validated destination directory open across this absolute rename.
    header.root_directory = None
    header.file_name_length = len(name)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, name, len(name))
    if not set_information(handle, _FILE_RENAME_INFO_CLASS, buffer, len(buffer)):
        _raise_windows_error("rename verified cleanup target", destination)


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


def _require_destination_directory(
    metadata: FileSystemMetadata, expected: ExactFileSnapshot
) -> None:
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.identity is None
        or metadata.volume_serial != expected.volume_serial
    ):
        raise ExactCleanupError(
            "destination is not an ordinary pinned directory on the source volume"
        )


def _require_quarantine_child(path: str, boundary: ExactRootBoundary) -> None:
    directory = Path(path)
    boundary_path = Path(_ordinary_absolute_path(boundary.path, "approved root"))
    if os.path.normcase(os.path.normpath(str(directory.parent))) != os.path.normcase(
        os.path.normpath(str(boundary_path))
    ):
        raise ExactCleanupError("quarantine directory must be a direct approved-root child")
    expected_prefix = f"{QUARANTINE_DIRECTORY_NAME}-batch_"
    if not directory.name.casefold().startswith(expected_prefix.casefold()):
        raise ExactCleanupError("quarantine directory has an invalid batch namespace")


def _verify_open_quarantine_directory(path: str, root_final: str) -> None:
    handle = _open_exact_directory(path)
    try:
        metadata = read_file_metadata_handle(handle)
        if (
            not metadata.is_directory
            or metadata.is_reparse_point
            or metadata.is_cloud_placeholder
        ):
            raise ExactCleanupError("quarantine directory is not an ordinary directory")
        _require_handle_in_boundary(handle, root_final, allow_equal=False)
        final = _final_path(handle)
        if os.path.dirname(final) != root_final:
            raise ExactCleanupError("quarantine directory is not a direct boundary child")
        if not audit_private_directory(Path(path)).policy_satisfied:
            raise ExactCleanupError("quarantine directory private DACL verification failed")
    finally:
        _close_handle(handle)


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
    "QUARANTINE_DIRECTORY_NAME",
    "ExactCleanupError",
    "ExactFileSnapshot",
    "ExactMutationResult",
    "ExactRootBoundary",
    "metadata_matches_snapshot",
    "prepare_private_quarantine_directory",
    "purge_exact_file",
    "quarantine_exact_file",
    "restore_exact_file",
    "verify_private_quarantine_directory",
]
