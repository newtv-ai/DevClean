"""Small Win32 Known Folder helpers used by destructive-path discovery."""

from __future__ import annotations

import ctypes
import os
import uuid
from pathlib import Path

_FOLDERID_LOCAL_APPDATA = "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"
_COINIT_APARTMENTTHREADED = 0x2
_S_OK = 0
_S_FALSE = 1
_RPC_E_CHANGED_MODE = ctypes.c_long(0x80010106).value
_WINDOWS = os.name == "nt"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def local_appdata_path() -> Path:
    """Return the current user's Local AppData from the Windows Known Folder API.

    Environment variables are intentionally not used as destructive path authority.
    Known folders can themselves be redirected; callers must still enforce their own
    local-volume/reparse/storage boundary before granting mutation authority.
    """

    if not _WINDOWS:
        raise RuntimeError("Windows Known Folder API is only available on Windows")

    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)

    co_initialize_ex = ole32.CoInitializeEx
    co_initialize_ex.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    co_initialize_ex.restype = ctypes.c_long
    co_uninitialize = ole32.CoUninitialize
    co_uninitialize.argtypes = []
    co_uninitialize.restype = None
    co_task_mem_free = ole32.CoTaskMemFree
    co_task_mem_free.argtypes = [ctypes.c_void_p]
    co_task_mem_free.restype = None

    sh_get_known_folder_path = shell32.SHGetKnownFolderPath
    sh_get_known_folder_path.argtypes = [
        ctypes.POINTER(_GUID),
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    sh_get_known_folder_path.restype = ctypes.c_long

    initialized = False
    init_result = int(co_initialize_ex(None, _COINIT_APARTMENTTHREADED))
    if init_result in {_S_OK, _S_FALSE}:
        initialized = True
    elif init_result != _RPC_E_CHANGED_MODE:
        raise RuntimeError(f"CoInitializeEx failed for Known Folder lookup: 0x{init_result & 0xFFFFFFFF:08X}")

    guid = _guid(_FOLDERID_LOCAL_APPDATA)
    pointer = ctypes.c_void_p()
    try:
        result = int(
            sh_get_known_folder_path(
                ctypes.byref(guid),
                0,
                None,
                ctypes.byref(pointer),
            )
        )
        if result < 0 or pointer.value is None:
            raise RuntimeError(
                "SHGetKnownFolderPath(FOLDERID_LocalAppData) failed: "
                f"0x{result & 0xFFFFFFFF:08X}"
            )
        try:
            value = ctypes.wstring_at(pointer.value)
        finally:
            co_task_mem_free(pointer)
    finally:
        if initialized:
            co_uninitialize()

    if not value or "\x00" in value:
        raise RuntimeError("Windows returned an invalid Local AppData known-folder path")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise RuntimeError("Windows returned a non-absolute Local AppData known-folder path")
    return Path(os.path.abspath(candidate))


def _guid(value: str) -> _GUID:
    parsed = uuid.UUID(value)
    data4 = (ctypes.c_ubyte * 8)(*parsed.bytes[8:])
    return _GUID(parsed.time_low, parsed.time_mid, parsed.time_hi_version, data4)


__all__ = ["local_appdata_path"]
