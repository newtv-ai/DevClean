"""Windows token inspection and fail-closed private-directory ACL helpers."""

from __future__ import annotations

import ctypes
import os
import stat
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from reclaimer.platform.windows.filesystem import (
    FILE_ATTRIBUTE_REPARSE_POINT,
    read_file_metadata,
)
from reclaimer.platform.windows.volumes import is_local_fixed_path

TOKEN_QUERY = 0x0008
TOKEN_ELEVATION_CLASS = 20
_TOKEN_USER_CLASS = 1
_ERROR_INSUFFICIENT_BUFFER = 122
_READ_CONTROL = 0x00020000
_WRITE_DAC = 0x00040000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_ATTRIBUTE_DIRECTORY = 0x00000010
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_OPEN_NO_RECALL = 0x00100000
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_SE_FILE_OBJECT = 1
_OWNER_SECURITY_INFORMATION = 0x00000001
_DACL_SECURITY_INFORMATION = 0x00000004
_PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
_SE_DACL_PRESENT = 0x0004
_SE_DACL_PROTECTED = 0x1000
_ACCESS_ALLOWED_ACE_TYPE = 0
_ACCESS_DENIED_ACE_TYPE = 1
_OBJECT_INHERIT_ACE = 0x01
_CONTAINER_INHERIT_ACE = 0x02
_INHERITED_ACE = 0x10
_FILE_ALL_ACCESS = 0x001F01FF
_SDDL_REVISION_1 = 1
_SYSTEM_SID = "S-1-5-18"
_BUILTIN_ADMINISTRATORS_SID = "S-1-5-32-544"


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("sid", wintypes.LPVOID), ("attributes", wintypes.DWORD)]


class _TokenUser(ctypes.Structure):
    _fields_ = [("user", _SidAndAttributes)]


class _ByHandleFileInformation(ctypes.Structure):
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


class _Acl(ctypes.Structure):
    _fields_ = [
        ("revision", ctypes.c_ubyte),
        ("sbz1", ctypes.c_ubyte),
        ("size", wintypes.WORD),
        ("ace_count", wintypes.WORD),
        ("sbz2", wintypes.WORD),
    ]


class _AceHeader(ctypes.Structure):
    _fields_ = [
        ("ace_type", ctypes.c_ubyte),
        ("ace_flags", ctypes.c_ubyte),
        ("ace_size", wintypes.WORD),
    ]


@dataclass(frozen=True, slots=True)
class DirectoryAceAudit:
    sid: str | None
    ace_type: int
    access_mask: int
    ace_flags: int

    @property
    def inherited(self) -> bool:
        return bool(self.ace_flags & _INHERITED_ACE)

    @property
    def grants_full_control(self) -> bool:
        return (
            self.ace_type == _ACCESS_ALLOWED_ACE_TYPE
            and self.access_mask & _FILE_ALL_ACCESS == _FILE_ALL_ACCESS
        )


@dataclass(frozen=True, slots=True)
class PrivateDirectoryAudit:
    path: str
    platform: str
    protected: bool
    dacl_present: bool
    owner_sid: str | None
    current_user_sid: str | None
    entries: tuple[DirectoryAceAudit, ...] = ()
    posix_mode: int | None = None

    @property
    def allowed_sids(self) -> tuple[str, ...]:
        return tuple(
            entry.sid
            for entry in self.entries
            if entry.ace_type == _ACCESS_ALLOWED_ACE_TYPE and entry.sid is not None
        )

    @property
    def expected_sids(self) -> tuple[str, ...]:
        if self.current_user_sid is None:
            return ()
        return _unique_sids(
            self.current_user_sid,
            _SYSTEM_SID,
            _BUILTIN_ADMINISTRATORS_SID,
        )

    @property
    def policy_satisfied(self) -> bool:
        if self.platform != "windows":
            return self.posix_mode == 0o700
        expected = set(self.expected_sids)
        if not self.protected or not self.dacl_present or not expected:
            return False
        if len(self.entries) != len(expected):
            return False
        for entry in self.entries:
            if (
                entry.sid not in expected
                or entry.ace_type != _ACCESS_ALLOWED_ACE_TYPE
                or not entry.grants_full_control
                or entry.inherited
                or entry.ace_flags
                & (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
                != (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
            ):
                return False
        return set(self.allowed_sids) == expected


@dataclass(frozen=True, slots=True)
class PrivateFileAudit:
    """Security classification for one non-directory private state file."""

    path: str
    platform: str
    protected: bool
    dacl_present: bool
    owner_sid: str | None
    current_user_sid: str | None
    entries: tuple[DirectoryAceAudit, ...] = ()
    posix_mode: int | None = None

    @property
    def allowed_sids(self) -> tuple[str, ...]:
        return tuple(
            entry.sid
            for entry in self.entries
            if entry.ace_type == _ACCESS_ALLOWED_ACE_TYPE and entry.sid is not None
        )

    @property
    def expected_sids(self) -> tuple[str, ...]:
        if self.current_user_sid is None:
            return ()
        return _unique_sids(
            self.current_user_sid,
            _SYSTEM_SID,
            _BUILTIN_ADMINISTRATORS_SID,
        )

    @property
    def policy_satisfied(self) -> bool:
        if self.platform != "windows":
            return self.posix_mode == 0o600
        expected = set(self.expected_sids)
        if not self.protected or not self.dacl_present or not expected:
            return False
        if len(self.entries) != len(expected):
            return False
        for entry in self.entries:
            if (
                entry.sid not in expected
                or entry.ace_type != _ACCESS_ALLOWED_ACE_TYPE
                or not entry.grants_full_control
                or entry.inherited
                or entry.ace_flags & (_OBJECT_INHERIT_ACE | _CONTAINER_INHERIT_ACE)
            ):
                return False
        return set(self.allowed_sids) == expected


@dataclass(frozen=True, slots=True)
class _WindowsPrivateAclAudit:
    path: str
    protected: bool
    dacl_present: bool
    owner_sid: str | None
    current_user_sid: str
    entries: tuple[DirectoryAceAudit, ...]


class _TokenElevation(ctypes.Structure):
    _fields_ = [("TokenIsElevated", wintypes.DWORD)]


def is_process_elevated() -> bool:
    """Return whether the current process token is elevated.

    Reclaimer's main process must exit when this is true. Group membership is intentionally not
    used because an administrator can still be running with a filtered, non-elevated token.
    """

    if os.name != "nt":
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())

    try:
        elevation = _TokenElevation()
        returned = wintypes.DWORD()
        if not advapi32.GetTokenInformation(
            token,
            TOKEN_ELEVATION_CLASS,
            ctypes.byref(elevation),
            ctypes.sizeof(elevation),
            ctypes.byref(returned),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        return bool(elevation.TokenIsElevated)
    finally:
        kernel32.CloseHandle(token)


def secure_private_directory(path: Path) -> PrivateDirectoryAudit:
    """Replace one directory DACL with the exact Reclaimer private policy.

    Windows grants inheritable full control only to the current token user,
    LocalSystem, and Builtin Administrators, and protects the DACL from parent
    inheritance.  The owner is never changed and no privilege is enabled.  A
    service account therefore secures the directory for that service identity,
    not for an unrelated interactive user.

    On non-Windows platforms the minimal fallback is an owner-only ``0o700``
    mode.  Every validation or API failure is propagated; there is no permissive
    fallback on Windows.
    """

    directory = _validated_directory(path)
    if os.name != "nt":
        os.chmod(directory, 0o700, follow_symlinks=False)
        audit = audit_private_directory(directory)
    else:
        current_sid = _current_user_sid_string()
        _set_windows_private_dacl(directory, current_sid)
        audit = _audit_windows_private_directory(directory, current_sid)
    if not audit.policy_satisfied:
        raise RuntimeError("private-directory security policy verification failed")
    return audit


def audit_private_directory(path: Path) -> PrivateDirectoryAudit:
    """Read and classify directory security without changing it."""

    directory = _validated_directory(path)
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(directory, follow_symlinks=False).st_mode)
        return PrivateDirectoryAudit(
            path=str(directory),
            platform="posix",
            protected=mode & 0o077 == 0,
            dacl_present=False,
            owner_sid=None,
            current_user_sid=None,
            posix_mode=mode,
        )
    current_sid = _current_user_sid_string()
    return _audit_windows_private_directory(directory, current_sid)


def secure_private_file(path: Path) -> PrivateFileAudit:
    """Apply and verify the exact private policy on one ordinary state file.

    The file DACL is protected and intentionally has no inheritance flags.  Existing
    hard-linked files are rejected because changing or using one link would affect a
    potentially unrelated name outside the private directory.
    """

    ordinary_file = _validated_file(path)
    if os.name != "nt":
        os.chmod(ordinary_file, 0o600, follow_symlinks=False)
        audit = audit_private_file(ordinary_file)
    else:
        current_sid = _current_user_sid_string()
        _set_windows_private_file_dacl(ordinary_file, current_sid)
        audit = _audit_windows_private_file(ordinary_file, current_sid)
    if not audit.policy_satisfied:
        raise RuntimeError("private-file security policy verification failed")
    return audit


def audit_private_file(path: Path) -> PrivateFileAudit:
    """Read and classify one ordinary file's security without changing it."""

    ordinary_file = _validated_file(path)
    if os.name != "nt":
        mode = stat.S_IMODE(os.stat(ordinary_file, follow_symlinks=False).st_mode)
        return PrivateFileAudit(
            path=str(ordinary_file),
            platform="posix",
            protected=mode & 0o077 == 0,
            dacl_present=False,
            owner_sid=None,
            current_user_sid=None,
            posix_mode=mode,
        )
    current_sid = _current_user_sid_string()
    return _audit_windows_private_file(ordinary_file, current_sid)


def _validated_directory(path: Path) -> Path:
    directory = Path(path)
    if not directory.is_absolute():
        raise ValueError("private directory path must be absolute")
    if not is_local_fixed_path(directory):
        raise ValueError(
            "private directory must be on a fixed local volume with no reparse ancestors"
        )
    metadata = read_file_metadata(directory)
    if not metadata.is_directory:
        raise ValueError("private directory path must reference an existing directory")
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise ValueError("private directory must not be a reparse or Cloud Files boundary")
    return directory


def _validated_file(path: Path) -> Path:
    ordinary_file = Path(path)
    if not ordinary_file.is_absolute():
        raise ValueError("private file path must be absolute")
    if not is_local_fixed_path(ordinary_file):
        raise ValueError(
            "private file must be on a fixed local volume with no reparse ancestors"
        )
    metadata = read_file_metadata(ordinary_file)
    if metadata.is_directory:
        raise ValueError("private file path must reference an ordinary file")
    if metadata.is_reparse_point or metadata.is_cloud_placeholder:
        raise ValueError("private file must not be a reparse or Cloud Files boundary")
    if metadata.link_count not in {None, 1}:
        raise ValueError("private file must not have multiple hard links")
    return ordinary_file


def _set_windows_private_dacl(directory: Path, current_sid: str) -> None:
    expected = _unique_sids(
        current_sid,
        _SYSTEM_SID,
        _BUILTIN_ADMINISTRATORS_SID,
    )
    sddl = "D:P" + "".join(f"(A;OICI;FA;;;{sid})" for sid in expected)
    _set_windows_private_acl(directory, sddl, require_directory=True)


def _set_windows_private_file_dacl(ordinary_file: Path, current_sid: str) -> None:
    expected = _unique_sids(
        current_sid,
        _SYSTEM_SID,
        _BUILTIN_ADMINISTRATORS_SID,
    )
    sddl = "D:P" + "".join(f"(A;;FA;;;{sid})" for sid in expected)
    _set_windows_private_acl(ordinary_file, sddl, require_directory=False)


def _set_windows_private_acl(
    path: Path, sddl: str, *, require_directory: bool
) -> None:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert_descriptor = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert_descriptor.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert_descriptor.restype = wintypes.BOOL
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_dacl.restype = wintypes.BOOL
    set_security = advapi32.SetSecurityInfo
    set_security.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    set_security.restype = wintypes.DWORD
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL

    descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not convert_descriptor(
        sddl,
        _SDDL_REVISION_1,
        ctypes.byref(descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        present = wintypes.BOOL()
        defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not get_dacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if not present.value or not dacl:
            raise RuntimeError("generated private security descriptor has no DACL")

        handle = _open_path_handle(
            path,
            write_dac=True,
            require_directory=require_directory,
        )
        try:
            status = int(
                set_security(
                    handle,
                    _SE_FILE_OBJECT,
                    _DACL_SECURITY_INFORMATION
                    | _PROTECTED_DACL_SECURITY_INFORMATION,
                    None,
                    None,
                    dacl,
                    None,
                )
            )
            if status:
                raise ctypes.WinError(status)
        finally:
            _close_windows_handle(handle)
    finally:
        kernel32.LocalFree(descriptor)


def _audit_windows_private_directory(
    directory: Path, current_sid: str
) -> PrivateDirectoryAudit:
    audit = _read_windows_private_acl(
        directory,
        current_sid,
        require_directory=True,
    )
    return PrivateDirectoryAudit(
        path=audit.path,
        platform="windows",
        protected=audit.protected,
        dacl_present=audit.dacl_present,
        owner_sid=audit.owner_sid,
        current_user_sid=audit.current_user_sid,
        entries=audit.entries,
    )


def _audit_windows_private_file(
    ordinary_file: Path, current_sid: str
) -> PrivateFileAudit:
    audit = _read_windows_private_acl(
        ordinary_file,
        current_sid,
        require_directory=False,
    )
    return PrivateFileAudit(
        path=audit.path,
        platform="windows",
        protected=audit.protected,
        dacl_present=audit.dacl_present,
        owner_sid=audit.owner_sid,
        current_user_sid=audit.current_user_sid,
        entries=audit.entries,
    )


def _read_windows_private_acl(
    path: Path, current_sid: str, *, require_directory: bool
) -> _WindowsPrivateAclAudit:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_security = advapi32.GetSecurityInfo
    get_security.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_security.restype = wintypes.DWORD
    get_control = advapi32.GetSecurityDescriptorControl
    get_control.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    )
    get_control.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL

    handle = _open_path_handle(
        path,
        write_dac=False,
        require_directory=require_directory,
    )
    descriptor = wintypes.LPVOID()
    owner = wintypes.LPVOID()
    dacl = wintypes.LPVOID()
    try:
        status = int(
            get_security(
                handle,
                _SE_FILE_OBJECT,
                _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
        )
        if status:
            raise ctypes.WinError(status)
        control = wintypes.WORD()
        revision = wintypes.DWORD()
        if not get_control(descriptor, ctypes.byref(control), ctypes.byref(revision)):
            raise ctypes.WinError(ctypes.get_last_error())
        owner_sid = _sid_to_string(owner) if owner else None
        entries = _enumerate_acl(dacl) if dacl else ()
        return _WindowsPrivateAclAudit(
            path=str(path),
            protected=bool(control.value & _SE_DACL_PROTECTED),
            dacl_present=bool(control.value & _SE_DACL_PRESENT) and bool(dacl),
            owner_sid=owner_sid,
            current_user_sid=current_sid,
            entries=entries,
        )
    finally:
        if descriptor:
            kernel32.LocalFree(descriptor)
        _close_windows_handle(handle)


def _enumerate_acl(dacl: wintypes.LPVOID) -> tuple[DirectoryAceAudit, ...]:
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    get_ace = advapi32.GetAce
    get_ace.argtypes = (
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
    )
    get_ace.restype = wintypes.BOOL
    acl = ctypes.cast(dacl, ctypes.POINTER(_Acl)).contents
    if acl.ace_count > 4096:
        raise ValueError("directory DACL contains too many ACEs")

    entries: list[DirectoryAceAudit] = []
    for index in range(acl.ace_count):
        raw_ace = wintypes.LPVOID()
        if not get_ace(dacl, index, ctypes.byref(raw_ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        raw_address = raw_ace.value
        if raw_address is None:
            raise ValueError("directory DACL contains a null ACE pointer")
        address = int(raw_address)
        header = ctypes.cast(raw_ace, ctypes.POINTER(_AceHeader)).contents
        if header.ace_size < 8:
            raise ValueError("directory DACL contains a truncated ACE")
        access_mask = ctypes.c_uint32.from_address(
            address + ctypes.sizeof(_AceHeader)
        ).value
        sid: str | None = None
        if header.ace_type in {_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_DENIED_ACE_TYPE}:
            if header.ace_size < 12:
                raise ValueError("directory DACL contains a truncated access ACE")
            sid = _sid_to_string(
                wintypes.LPVOID(address + ctypes.sizeof(_AceHeader) + 4)
            )
        entries.append(
            DirectoryAceAudit(
                sid=sid,
                ace_type=int(header.ace_type),
                access_mask=int(access_mask),
                ace_flags=int(header.ace_flags),
            )
        )
    return tuple(entries)


def _open_directory_handle(directory: Path, *, write_dac: bool) -> int:
    return _open_path_handle(
        directory,
        write_dac=write_dac,
        require_directory=True,
    )


def _open_path_handle(
    path: Path, *, write_dac: bool, require_directory: bool
) -> int:
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
    get_information = kernel32.GetFileInformationByHandle
    get_information.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(_ByHandleFileInformation),
    )
    get_information.restype = wintypes.BOOL

    access = _READ_CONTROL | (_WRITE_DAC if write_dac else 0)
    raw_handle = create_file(
        str(path),
        access,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        (_FILE_FLAG_BACKUP_SEMANTICS if require_directory else 0)
        | _FILE_FLAG_OPEN_REPARSE_POINT
        | _FILE_FLAG_OPEN_NO_RECALL,
        None,
    )
    if not raw_handle or raw_handle == _INVALID_HANDLE_VALUE:
        raise ctypes.WinError(ctypes.get_last_error())
    handle = int(raw_handle)
    information = _ByHandleFileInformation()
    if not get_information(handle, ctypes.byref(information)):
        error = ctypes.WinError(ctypes.get_last_error())
        _close_windows_handle(handle)
        raise error
    is_directory = bool(information.file_attributes & _FILE_ATTRIBUTE_DIRECTORY)
    if require_directory and not is_directory:
        _close_windows_handle(handle)
        raise ValueError("private directory handle does not reference a directory")
    if not require_directory and is_directory:
        _close_windows_handle(handle)
        raise ValueError("private file handle references a directory")
    if information.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        _close_windows_handle(handle)
        raise ValueError("private directory handle references a reparse point")
    return handle


def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _current_user_sid_string() -> str:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL

    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(
        kernel32.GetCurrentProcess(), TOKEN_QUERY, ctypes.byref(token)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        required = wintypes.DWORD()
        advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            None,
            0,
            ctypes.byref(required),
        )
        error = ctypes.get_last_error()
        if error != _ERROR_INSUFFICIENT_BUFFER or required.value == 0:
            raise ctypes.WinError(error)
        buffer = ctypes.create_string_buffer(required.value)
        if not advapi32.GetTokenInformation(
            token,
            _TOKEN_USER_CLASS,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_to_string(token_user.user.sid)
    finally:
        kernel32.CloseHandle(token)


def _sid_to_string(sid: wintypes.LPVOID) -> str:
    if not sid:
        raise ValueError("SID pointer is null")
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32.IsValidSid.argtypes = (wintypes.LPVOID,)
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.ConvertSidToStringSidW.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.LPWSTR),
    )
    advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = (wintypes.HLOCAL,)
    kernel32.LocalFree.restype = wintypes.HLOCAL
    if not advapi32.IsValidSid(sid):
        raise ValueError("security descriptor contains an invalid SID")
    text = wintypes.LPWSTR()
    if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return str(text.value)
    finally:
        kernel32.LocalFree(text)


def _unique_sids(*sids: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(sids))


__all__ = [
    "DirectoryAceAudit",
    "PrivateDirectoryAudit",
    "PrivateFileAudit",
    "audit_private_directory",
    "audit_private_file",
    "is_process_elevated",
    "secure_private_directory",
    "secure_private_file",
]
