"""Windows Recycle Bin bridge used by the explicit local recycle workflow."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from os import PathLike

_FO_DELETE = 0x0003
_FOF_ALLOWUNDO = 0x0040
_FOF_NORECURSION = 0x1000
_FOF_NO_CONNECTED_ELEMENTS = 0x2000
_FOF_WANTNUKEWARNING = 0x4000
_FOF_NORECURSEREPARSE = 0x8000


class RecycleBinError(RuntimeError):
    """The shell did not complete a requested Recycle Bin operation."""


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("wFunc", wintypes.UINT),
        ("pFrom", wintypes.LPCWSTR),
        ("pTo", wintypes.LPCWSTR),
        ("fFlags", ctypes.c_ushort),
        ("fAnyOperationsAborted", wintypes.BOOL),
        ("hNameMappings", wintypes.LPVOID),
        ("lpszProgressTitle", wintypes.LPCWSTR),
    ]


def _absolute_source(path: str | PathLike[str]) -> str:
    source = os.path.abspath(os.fspath(path))
    if not os.path.isabs(source) or source.startswith("\\\\?\\") or "\x00" in source:
        raise RecycleBinError("Recycle Bin source must be an ordinary absolute Windows path")
    return source


def recycle_file(path: str | PathLike[str]) -> None:
    """Send one fully-qualified regular-file path to the Windows Recycle Bin.

    The caller must already have verified the object identity and rejected
    directories/reparse points.  This function neither calls ``DeleteFile`` nor
    passes silent/no-confirmation flags.  Windows is asked to warn instead of
    silently nuking an object that cannot be placed in the Recycle Bin.
    """

    if os.name != "nt":
        raise RecycleBinError("Windows Recycle Bin operations require Windows")
    source = _absolute_source(path)
    source_buffer = source + "\x00\x00"

    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    operation = shell32.SHFileOperationW
    operation.argtypes = (ctypes.POINTER(_SHFILEOPSTRUCTW),)
    operation.restype = ctypes.c_int
    request = _SHFILEOPSTRUCTW(
        None,
        _FO_DELETE,
        source_buffer,
        None,
        _FOF_ALLOWUNDO
        | _FOF_NORECURSION
        | _FOF_NO_CONNECTED_ELEMENTS
        | _FOF_WANTNUKEWARNING
        | _FOF_NORECURSEREPARSE,
        False,
        None,
        None,
    )
    result = int(operation(ctypes.byref(request)))
    if result != 0:
        raise RecycleBinError(f"Windows shell Recycle Bin operation failed: 0x{result:04x}")
    if request.fAnyOperationsAborted:
        raise RecycleBinError("Windows shell Recycle Bin operation was aborted")


__all__ = ["RecycleBinError", "recycle_file"]
