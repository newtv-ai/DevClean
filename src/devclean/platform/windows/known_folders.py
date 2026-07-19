"""Canonical Windows provenance for the two permanent-cleanup roots.

Environment variables and caller-provided catalog entries are intentionally
not consulted.  A permanent root must come from Windows APIs, match the
expected LocalAppData structure, already exist, and have an ordinary stable
identity on a local fixed volume.  Failure simply disables permanent cleanup.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from uuid import UUID

from devclean.platform.windows.filesystem import read_file_metadata
from devclean.platform.windows.volumes import is_local_fixed_path

_FOLDERID_LOCAL_APPDATA = UUID("f1b32785-6fba-4fcf-9d55-7b8e7f157091")
_MAX_PATH_TEXT = 32_767


class CanonicalCleanupKind(StrEnum):
    USER_TEMP = "USER_TEMP"
    CRASH_DUMPS = "CRASH_DUMPS"


@dataclass(frozen=True, slots=True)
class CanonicalCleanupRoot:
    path: Path
    kind: CanonicalCleanupKind


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]


def canonical_permanent_cleanup_roots() -> tuple[CanonicalCleanupRoot, ...]:
    """Return only API-derived, structurally constrained per-user roots."""

    if os.name != "nt":
        return ()
    try:
        local_appdata = _validated_root(_windows_local_appdata())
    except (OSError, RuntimeError, ValueError):
        return ()
    roots: list[CanonicalCleanupRoot] = []
    try:
        temp = _validated_root(_windows_temp_path2())
    except (OSError, RuntimeError, ValueError):
        temp = None
    expected_temp = local_appdata / "Temp"
    if temp is not None and _normalized(temp) == _normalized(expected_temp):
        roots.append(CanonicalCleanupRoot(temp, CanonicalCleanupKind.USER_TEMP))
    crash_dumps = local_appdata / "CrashDumps"
    try:
        crash_dumps = _validated_root(crash_dumps)
    except (OSError, RuntimeError, ValueError):
        pass
    else:
        # The root is the exact KnownFolder-relative directory, never a caller
        # supplied descendant or an environment-derived alternative.
        if _normalized(crash_dumps.parent) == _normalized(local_appdata):
            roots.append(
                CanonicalCleanupRoot(crash_dumps, CanonicalCleanupKind.CRASH_DUMPS)
            )
    return tuple(roots)


def _windows_temp_path2() -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    try:
        get_temp = kernel32.GetTempPath2W
    except AttributeError as error:
        raise OSError("GetTempPath2W is unavailable") from error
    get_temp.argtypes = (wintypes.DWORD, wintypes.LPWSTR)
    get_temp.restype = wintypes.DWORD
    capacity = _MAX_PATH_TEXT + 1
    buffer = ctypes.create_unicode_buffer(capacity)
    length = int(get_temp(capacity, buffer))
    if length == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    if length >= capacity:
        raise OSError("GetTempPath2W returned an overlong path")
    return Path(buffer.value)


def _windows_local_appdata() -> Path:
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    get_path = shell32.SHGetKnownFolderPath
    get_path.argtypes = (
        ctypes.POINTER(_GUID),
        wintypes.DWORD,
        wintypes.HANDLE,
        ctypes.POINTER(ctypes.c_wchar_p),
    )
    get_path.restype = ctypes.c_long
    ole32.CoTaskMemFree.argtypes = (wintypes.LPVOID,)
    ole32.CoTaskMemFree.restype = None
    guid = _guid(_FOLDERID_LOCAL_APPDATA)
    output = ctypes.c_wchar_p()
    result = int(get_path(ctypes.byref(guid), 0, None, ctypes.byref(output)))
    if result != 0 or not output.value:
        raise OSError(result, "SHGetKnownFolderPath(LocalAppData) failed")
    try:
        return Path(output.value)
    finally:
        ole32.CoTaskMemFree(output)


def _validated_root(path: Path) -> Path:
    root = Path(os.path.abspath(path))
    if (
        not root.is_absolute()
        or len(str(root)) > _MAX_PATH_TEXT
        or root == Path(root.anchor)
        or not is_local_fixed_path(root)
    ):
        raise ValueError("canonical cleanup root failed its structural boundary")
    metadata = read_file_metadata(root)
    if (
        not metadata.is_directory
        or metadata.is_reparse_point
        or metadata.is_cloud_placeholder
        or metadata.identity is None
    ):
        raise ValueError("canonical cleanup root lacks an ordinary stable identity")
    return root


def _guid(value: UUID) -> _GUID:
    node = value.node.to_bytes(6, "big")
    tail = bytes((value.clock_seq_hi_variant, value.clock_seq_low)) + node
    return _GUID(
        value.time_low,
        value.time_mid,
        value.time_hi_version,
        (ctypes.c_ubyte * 8)(*tail),
    )


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


__all__ = [
    "CanonicalCleanupKind",
    "CanonicalCleanupRoot",
    "canonical_permanent_cleanup_roots",
]
