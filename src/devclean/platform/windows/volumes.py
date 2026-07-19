"""Windows volume classification for local application state boundaries."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from enum import IntEnum
from pathlib import Path

from devclean.platform.windows.filesystem import FILE_ATTRIBUTE_REPARSE_POINT


class DriveType(IntEnum):
    UNKNOWN = 0
    NO_ROOT_DIR = 1
    REMOVABLE = 2
    FIXED = 3
    REMOTE = 4
    CDROM = 5
    RAMDISK = 6


def drive_type(path: Path) -> DriveType:
    """Classify the volume containing *path* without creating the path."""

    absolute = Path(os.path.abspath(path))
    text = str(absolute)
    if text.startswith((r"\\", "//")):
        return DriveType.REMOTE
    if os.name != "nt":
        return DriveType.FIXED
    anchor = absolute.anchor
    if not anchor:
        return DriveType.UNKNOWN
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_drive_type = kernel32.GetDriveTypeW
    get_drive_type.argtypes = (wintypes.LPCWSTR,)
    get_drive_type.restype = wintypes.UINT
    value = int(get_drive_type(anchor))
    try:
        return DriveType(value)
    except ValueError:
        return DriveType.UNKNOWN


def has_reparse_ancestor(path: Path) -> bool:
    """Return whether an existing path component redirects through a reparse point."""

    absolute = Path(os.path.abspath(path))
    candidate = absolute
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    anchor = Path(candidate.anchor) if candidate.anchor else candidate
    while candidate != anchor and candidate != candidate.parent:
        try:
            metadata = os.stat(candidate, follow_symlinks=False)
        except OSError:
            return True
        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT:
            return True
        candidate = candidate.parent
    return False


def is_local_fixed_path(path: Path) -> bool:
    """Require a fixed local volume and a non-reparse existing ancestor chain."""

    return drive_type(path) is DriveType.FIXED and not has_reparse_ancestor(path)


def fixed_volume_roots() -> tuple[Path, ...]:
    """Return currently mounted fixed local drive roots without probing their contents."""

    if os.name != "nt":
        anchor = Path.cwd().anchor
        return (Path(anchor),) if anchor else ()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_logical_drives = kernel32.GetLogicalDrives
    get_logical_drives.argtypes = ()
    get_logical_drives.restype = wintypes.DWORD
    mask = int(get_logical_drives())
    roots: list[Path] = []
    for index in range(26):
        if mask & (1 << index):
            root = Path(f"{chr(ord('A') + index)}:\\")
            if drive_type(root) is DriveType.FIXED:
                roots.append(root)
    return tuple(roots)


__all__ = [
    "DriveType",
    "drive_type",
    "fixed_volume_roots",
    "has_reparse_ancestor",
    "is_local_fixed_path",
]
