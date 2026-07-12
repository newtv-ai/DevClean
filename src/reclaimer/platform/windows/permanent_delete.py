"""Handle-checked permanent deletion for a narrow, approved cache rule only."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any, cast

from reclaimer.platform.windows.filesystem import FileSystemMetadata, read_file_metadata_handle
from reclaimer.platform.windows.volumes import is_local_fixed_path

_DELETE = 0x00010000
_FILE_READ_ATTRIBUTES = 0x00000080
_FILE_SHARE_READ = 0x00000001
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_OPEN_NO_RECALL = 0x00100000
_FILE_DISPOSITION_INFO_CLASS = 4
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PermanentDeleteRefusal(RuntimeError):
    """A file did not meet the narrow permanent-delete safety contract."""


class _FILE_DISPOSITION_INFO(ctypes.Structure):
    _fields_ = [("delete_file", wintypes.BOOL)]


def permanently_delete_verified_file(
    path: Path, *, approved_root: Path, expected: FileSystemMetadata
) -> None:
    """Delete a preclassified ordinary file only after handle-time revalidation.

    This is deliberately unsuitable for generic paths: callers must provide an
    approved cache root and the metadata snapshot acquired during the same scan.
    The function opens the file with DELETE access, rejects replacement/reparse
    changes, verifies the final handle path remains under the approved root, and
    uses FileDispositionInfo on that handle. It never falls back to ``os.remove``.
    """

    if os.name != "nt":
        raise PermanentDeleteRefusal("permanent cache cleanup requires Windows")
    _require_expected_snapshot(expected)
    source = Path(os.path.abspath(path))
    root = Path(os.path.abspath(approved_root))
    if not source.is_absolute() or not root.is_absolute() or not is_local_fixed_path(root):
        raise PermanentDeleteRefusal("approved cache root must be an ordinary fixed local path")

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    root_handle = _open_handle(
        kernel32,
        root,
        desired_access=_FILE_READ_ATTRIBUTES,
        flags=_FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT,
    )
    try:
        root_final = _final_path(kernel32, root_handle)
        root_metadata = read_file_metadata_handle(root_handle)
        if (
            not root_metadata.is_directory
            or root_metadata.is_reparse_point
            or root_metadata.is_cloud_placeholder
        ):
            raise PermanentDeleteRefusal("approved cleanup root is not an ordinary directory")
        file_handle = _open_handle(
            kernel32,
            source,
            desired_access=_DELETE | _FILE_READ_ATTRIBUTES,
            flags=_FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_OPEN_NO_RECALL,
        )
        try:
            final_path = _final_path(kernel32, file_handle)
            if not _is_descendant(final_path, root_final):
                raise PermanentDeleteRefusal("opened file escaped the approved cache root")
            actual = read_file_metadata_handle(file_handle)
            if not _matches(expected, actual):
                raise PermanentDeleteRefusal("file changed since classification; cleanup skipped")
            disposition = _FILE_DISPOSITION_INFO(True)
            set_info = kernel32.SetFileInformationByHandle
            set_info.argtypes = (
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            )
            set_info.restype = wintypes.BOOL
            if not set_info(
                file_handle,
                _FILE_DISPOSITION_INFO_CLASS,
                ctypes.byref(disposition),
                ctypes.sizeof(disposition),
            ):
                _raise_last_error(source)
        finally:
            _close_handle(kernel32, file_handle)
    finally:
        _close_handle(kernel32, root_handle)


def _require_expected_snapshot(expected: FileSystemMetadata) -> None:
    if (
        expected.is_directory
        or expected.is_reparse_point
        or expected.is_cloud_placeholder
        or expected.volume_serial is None
        or expected.file_id is None
        or expected.file_id_kind != "file_id_128"
        or expected.link_count != 1
        or expected.creation_time_ns is None
        or expected.last_write_time_ns is None
    ):
        raise PermanentDeleteRefusal("cache candidate lacks the required stable file snapshot")


def _matches(expected: FileSystemMetadata, actual: FileSystemMetadata) -> bool:
    return (
        not actual.is_directory
        and not actual.is_reparse_point
        and not actual.is_cloud_placeholder
        and actual.volume_serial == expected.volume_serial
        and actual.file_id == expected.file_id
        and actual.file_id_kind == expected.file_id_kind
        and actual.link_count == expected.link_count
        and actual.logical_size == expected.logical_size
        and actual.attributes == expected.attributes
        and actual.reparse_tag == expected.reparse_tag
        and actual.creation_time_ns == expected.creation_time_ns
        and actual.last_write_time_ns == expected.last_write_time_ns
    )


def _open_handle(
    kernel32: Any, path: Path, *, desired_access: int, flags: int
) -> wintypes.HANDLE:
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
        str(path),
        desired_access,
        _FILE_SHARE_READ,
        None,
        _OPEN_EXISTING,
        flags,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _raise_last_error(path)
    return cast(wintypes.HANDLE, handle)


def _final_path(kernel32: Any, handle: wintypes.HANDLE) -> str:
    get_final = kernel32.GetFinalPathNameByHandleW
    get_final.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    get_final.restype = wintypes.DWORD
    capacity = 1024
    while capacity <= 32768:
        buffer = ctypes.create_unicode_buffer(capacity)
        length = int(get_final(handle, buffer, capacity, 0))
        if length == 0:
            _raise_last_error("opened file")
        if length < capacity:
            return buffer.value
        capacity = length + 1
    raise PermanentDeleteRefusal("final cache path exceeds the supported safety bound")


def _is_descendant(path: str, root: str) -> bool:
    normalized_path = os.path.normcase(path.removeprefix("\\\\?\\"))
    normalized_root = os.path.normcase(root.removeprefix("\\\\?\\"))
    try:
        return os.path.commonpath((normalized_path, normalized_root)) == normalized_root
    except ValueError:
        return False


def _close_handle(kernel32: Any, handle: wintypes.HANDLE) -> None:
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _raise_last_error(path: str | Path) -> None:
    code = ctypes.get_last_error()
    raise PermanentDeleteRefusal(f"Windows operation failed for {path}: {code}")


__all__ = ["PermanentDeleteRefusal", "permanently_delete_verified_file"]
