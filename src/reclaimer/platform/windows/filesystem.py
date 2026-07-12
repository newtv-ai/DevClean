"""Read-only Windows file-system metadata primitives.

The functions in this module never request write/delete access and never enable
backup/restore privileges.  The Windows implementation opens the named object
itself (rather than a reparse-point target) and asks the kernel for handle-based
identity and allocation information.  A portable ``lstat`` fallback keeps the
scanner testable on non-Windows hosts and provides reduced-confidence metadata
when a particular Windows file system does not expose a stable file ID.
"""

from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path
from typing import Final

FILE_ATTRIBUTE_REPARSE_POINT: Final = 0x00000400
FILE_ATTRIBUTE_SPARSE_FILE: Final = 0x00000200
FILE_ATTRIBUTE_COMPRESSED: Final = 0x00000800
FILE_ATTRIBUTE_OFFLINE: Final = 0x00001000
FILE_ATTRIBUTE_RECALL_ON_OPEN: Final = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS: Final = 0x00400000

IO_REPARSE_TAG_CLOUD: Final = 0x9000001A
IO_REPARSE_TAG_ONEDRIVE: Final = 0x80000021

_CLOUD_ATTRIBUTE_MASK: Final = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)
# Microsoft reserves bits 12-15 for the numbered Cloud Files tag variants
# (IO_REPARSE_TAG_CLOUD_1 through IO_REPARSE_TAG_CLOUD_F).  Clear only that
# provider nibble before comparing with the base Cloud tag.
_CLOUD_TAG_MASK: Final = 0xFFFF0FFF


@dataclass(frozen=True, slots=True)
class FileSystemMetadata:
    """Metadata gathered without following the final reparse point.

    ``allocation_size`` is ``None`` when the platform cannot report allocation
    safely.  It is deliberately not replaced with logical size in that case.
    ``file_id`` is a hexadecimal kernel file identifier, scoped by
    ``volume_serial`` and qualified by ``file_id_kind``.
    """

    is_directory: bool
    logical_size: int
    allocation_size: int | None
    volume_serial: int | None
    file_id: str | None
    file_id_kind: str | None
    link_count: int | None
    attributes: int | None
    reparse_tag: int | None
    is_reparse_point: bool
    is_cloud_placeholder: bool
    creation_time_ns: int | None = None
    last_write_time_ns: int | None = None

    @property
    def identity(self) -> tuple[int, str] | None:
        """Return the volume-scoped identity when one is available."""

        if self.volume_serial is None or self.file_id is None:
            return None
        return (self.volume_serial, self.file_id)


def is_cloud_reparse_tag(tag: int | None) -> bool:
    """Return whether *tag* belongs to the Windows Cloud Files tag family."""

    if tag is None:
        return False
    return tag == IO_REPARSE_TAG_ONEDRIVE or (tag & _CLOUD_TAG_MASK) == IO_REPARSE_TAG_CLOUD


def is_cloud_placeholder(attributes: int | None, reparse_tag: int | None) -> bool:
    """Classify offline/recall-on-access objects as Cloud Files boundaries."""

    return bool((attributes or 0) & _CLOUD_ATTRIBUTE_MASK) or is_cloud_reparse_tag(
        reparse_tag
    )


class _FILE_ID_128(ctypes.Structure):
    _fields_ = [("identifier", ctypes.c_ubyte * 16)]


class _FILE_ID_INFO(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_ulonglong),
        ("file_id", _FILE_ID_128),
    ]


class _FILE_STANDARD_INFO(ctypes.Structure):
    _fields_ = [
        ("allocation_size", ctypes.c_longlong),
        ("end_of_file", ctypes.c_longlong),
        ("number_of_links", wintypes.DWORD),
        ("delete_pending", ctypes.c_ubyte),
        ("directory", ctypes.c_ubyte),
    ]


class _FILE_ATTRIBUTE_TAG_INFO(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("reparse_tag", wintypes.DWORD),
    ]


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("file_attributes", wintypes.DWORD),
        ("creation_time", wintypes.FILETIME),
        ("last_access_time", wintypes.FILETIME),
        ("last_write_time", wintypes.FILETIME),
        ("volume_serial_number", wintypes.DWORD),
        ("file_size_high", wintypes.DWORD),
        ("file_size_low", wintypes.DWORD),
        ("number_of_links", wintypes.DWORD),
        ("file_index_high", wintypes.DWORD),
        ("file_index_low", wintypes.DWORD),
    ]


_FILE_READ_ATTRIBUTES: Final = 0x0080
_FILE_SHARE_READ: Final = 0x00000001
_FILE_SHARE_WRITE: Final = 0x00000002
_FILE_SHARE_DELETE: Final = 0x00000004
_OPEN_EXISTING: Final = 3
_FILE_FLAG_BACKUP_SEMANTICS: Final = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT: Final = 0x00200000
_FILE_FLAG_OPEN_NO_RECALL: Final = 0x00100000
_FILE_STANDARD_INFO_CLASS: Final = 1
_FILE_ATTRIBUTE_TAG_INFO_CLASS: Final = 9
_FILE_ID_INFO_CLASS: Final = 18
_INVALID_HANDLE_VALUE: Final = ctypes.c_void_p(-1).value


def _portable_metadata(path: str) -> FileSystemMetadata:
    result = os.stat(path, follow_symlinks=False)
    attributes_obj = getattr(result, "st_file_attributes", None)
    attributes = int(attributes_obj) if attributes_obj is not None else None
    reparse_tag_obj = getattr(result, "st_reparse_tag", None)
    reparse_tag = int(reparse_tag_obj) if reparse_tag_obj is not None else None
    is_link = stat.S_ISLNK(result.st_mode)

    if is_link and attributes is None:
        attributes = FILE_ATTRIBUTE_REPARSE_POINT

    blocks_obj = getattr(result, "st_blocks", None)
    allocation_size = int(blocks_obj) * 512 if blocks_obj is not None else None
    file_id = f"{result.st_ino:x}" if result.st_ino else None
    volume_serial = int(result.st_dev) if file_id is not None else None
    link_count = int(result.st_nlink) if result.st_nlink else None
    reparse = is_link or bool((attributes or 0) & FILE_ATTRIBUTE_REPARSE_POINT)

    return FileSystemMetadata(
        is_directory=stat.S_ISDIR(result.st_mode),
        logical_size=int(result.st_size),
        allocation_size=allocation_size,
        volume_serial=volume_serial,
        file_id=file_id,
        file_id_kind="stat_ino" if file_id is not None else None,
        link_count=link_count,
        attributes=attributes,
        reparse_tag=reparse_tag,
        is_reparse_point=reparse,
        is_cloud_placeholder=is_cloud_placeholder(attributes, reparse_tag),
        creation_time_ns=int(result.st_ctime_ns),
        last_write_time_ns=int(result.st_mtime_ns),
    )


def _raise_last_windows_error(path: str) -> None:
    error_code = ctypes.get_last_error()
    raise OSError(error_code, ctypes.FormatError(error_code), path)


def _filetime_to_unix_ns(value: wintypes.FILETIME) -> int | None:
    """Convert a Windows FILETIME to Unix nanoseconds without floating-point loss."""

    raw = (int(value.dwHighDateTime) << 32) | int(value.dwLowDateTime)
    if raw == 0:
        return None
    unix_100ns = raw - 116_444_736_000_000_000
    return None if unix_100ns < 0 else unix_100ns * 100


def _windows_metadata(path: str) -> FileSystemMetadata:
    # WinDLL is absent on non-Windows Python builds, so resolve it lazily.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    get_info = kernel32.GetFileInformationByHandleEx
    get_info.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    get_info.restype = wintypes.BOOL

    get_basic_info = kernel32.GetFileInformationByHandle
    get_basic_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION)]
    get_basic_info.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    handle = create_file(
        path,
        _FILE_READ_ATTRIBUTES,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS
        | _FILE_FLAG_OPEN_REPARSE_POINT
        | _FILE_FLAG_OPEN_NO_RECALL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        _raise_last_windows_error(path)

    try:
        standard = _FILE_STANDARD_INFO()
        standard_ok = bool(
            get_info(
                handle,
                _FILE_STANDARD_INFO_CLASS,
                ctypes.byref(standard),
                ctypes.sizeof(standard),
            )
        )

        attribute_tag = _FILE_ATTRIBUTE_TAG_INFO()
        attribute_ok = bool(
            get_info(
                handle,
                _FILE_ATTRIBUTE_TAG_INFO_CLASS,
                ctypes.byref(attribute_tag),
                ctypes.sizeof(attribute_tag),
            )
        )

        identity = _FILE_ID_INFO()
        identity_ok = bool(
            get_info(
                handle,
                _FILE_ID_INFO_CLASS,
                ctypes.byref(identity),
                ctypes.sizeof(identity),
            )
        )

        basic = _BY_HANDLE_FILE_INFORMATION()
        basic_ok = bool(get_basic_info(handle, ctypes.byref(basic)))

        if not standard_ok and not basic_ok:
            _raise_last_windows_error(path)

        if attribute_ok:
            attributes: int | None = int(attribute_tag.file_attributes)
        elif basic_ok:
            attributes = int(basic.file_attributes)
        else:
            attributes = None
        raw_reparse_tag = int(attribute_tag.reparse_tag) if attribute_ok else 0
        reparse_tag = raw_reparse_tag if raw_reparse_tag else None

        if identity_ok and any(identity.file_id.identifier):
            volume_serial: int | None = int(identity.volume_serial_number)
            file_id: str | None = bytes(identity.file_id.identifier).hex()
            file_id_kind: str | None = "file_id_128"
        elif basic_ok:
            volume_serial = int(basic.volume_serial_number)
            file_index = (int(basic.file_index_high) << 32) | int(basic.file_index_low)
            file_id = f"{file_index:016x}" if file_index else None
            file_id_kind = "file_index_64" if file_id is not None else None
        else:
            volume_serial = None
            file_id = None
            file_id_kind = None

        if standard_ok:
            logical_size = max(0, int(standard.end_of_file))
            allocation_size: int | None = max(0, int(standard.allocation_size))
            link_count: int | None = int(standard.number_of_links)
            is_directory = bool(standard.directory)
        else:
            logical_size = (int(basic.file_size_high) << 32) | int(basic.file_size_low)
            allocation_size = None
            link_count = int(basic.number_of_links)
            is_directory = bool((attributes or 0) & 0x00000010)

        reparse = bool((attributes or 0) & FILE_ATTRIBUTE_REPARSE_POINT)
        return FileSystemMetadata(
            is_directory=is_directory,
            logical_size=logical_size,
            allocation_size=allocation_size,
            volume_serial=volume_serial,
            file_id=file_id,
            file_id_kind=file_id_kind,
            link_count=link_count,
            attributes=attributes,
            reparse_tag=reparse_tag,
            is_reparse_point=reparse,
            is_cloud_placeholder=is_cloud_placeholder(attributes, reparse_tag),
            creation_time_ns=_filetime_to_unix_ns(basic.creation_time) if basic_ok else None,
            last_write_time_ns=(
                _filetime_to_unix_ns(basic.last_write_time) if basic_ok else None
            ),
        )
    finally:
        close_handle(handle)


def read_file_metadata(path: str | os.PathLike[str]) -> FileSystemMetadata:
    """Read metadata for *path* without following its final reparse point.

    This function is observational.  It never requests mutation rights and does
    not retry with broader permissions when metadata is inaccessible.
    """

    normalized = os.fspath(Path(path))
    if os.name == "nt":
        return _windows_metadata(normalized)
    return _portable_metadata(normalized)


__all__ = [
    "FILE_ATTRIBUTE_COMPRESSED",
    "FILE_ATTRIBUTE_OFFLINE",
    "FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS",
    "FILE_ATTRIBUTE_RECALL_ON_OPEN",
    "FILE_ATTRIBUTE_REPARSE_POINT",
    "FILE_ATTRIBUTE_SPARSE_FILE",
    "FileSystemMetadata",
    "is_cloud_placeholder",
    "is_cloud_reparse_tag",
    "read_file_metadata",
]
