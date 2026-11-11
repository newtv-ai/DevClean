"""What an installer-looking file actually is, and who owns it.

A downloaded installer package is safe to delete wherever it sits: removing
the file never uninstalls anything. The same kind of file is not junk when an
installed program owns it (a setup kept for repair, an uninstaller) or when it
lives in a Windows installer cache. Deciding that needs evidence about the file
itself, not its folder or extension:

* package format: MSI files are OLE compound files, MSIX/APPX packages are ZIP
  archives with an Appx manifest, and installer EXEs carry their installer
  framework's data after the program image (NSIS, 7-Zip SFX), a WiX bundle
  section, or a version resource naming the framework or describing a setup;
* ownership: Windows' installed-programs list (``Uninstall`` registry keys)
  names each installed program's folder, uninstaller and icon file.

Only a few small, bounded reads are made; nothing is ever executed.
"""

# Chinese user-facing format names use fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from devclean.core._application_cleanup_impl import in_scan_snapshot, run_cached, snapshot_roots
from devclean.platform.windows.command_paths import normalize_path, program_and_paths
from devclean.platform.windows.registry import UninstallEntry, uninstall_entries

INSTALLER_PACKAGE_SUFFIXES = frozenset({".msi", ".msix", ".msixbundle", ".appx", ".appxbundle"})
_ZIP_HEAD_BYTES = 64 * 1024
_ZIP_TAIL_BYTES = 256 * 1024
_OVERLAY_BYTES = 64 * 1024
_VERSION_BYTES = 64 * 1024
_MAX_SECTIONS = 96
_MAX_RESOURCE_ENTRIES = 4096
_RT_VERSION = 16
_SUBDIRECTORY = 0x80000000
_MSI_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
_ZIP_MAGIC = b"PK\x03\x04"
_APPX_MARKERS = (b"AppxManifest.xml", b"AppxBundleManifest.xml", b"AppxBlockMap.xml")
# Installer data appended after the program image (the "overlay"). Only the
# overlay is searched: tools such as the NSIS compiler carry the same strings
# inside their own code.
_OVERLAY_MARKERS = (
    (b"NullsoftInst", "NSIS 安装程序"),
    (b";!@Install@!UTF-8!", "7-Zip 自解压安装程序"),
)
# Installer framework names written into the installer's version information.
_VERSION_MARKERS = (
    ("Inno Setup", "Inno Setup 安装程序"),
    ("InstallShield", "InstallShield 安装程序"),
    ("Advanced Installer", "Advanced Installer 安装程序"),
    ("Nullsoft Install System", "NSIS 安装程序"),
)
_VERSION_KEYS = ("FileDescription", "ProductName", "OriginalFilename", "InternalName")
_SETUP_WORDS = re.compile(r"\b(?:setup|installer)\b|安装程序|安装向导", re.IGNORECASE)
_UNINSTALL_WORDS = re.compile(r"uninstall|卸载", re.IGNORECASE)
_UNINSTALLER_NAME = re.compile(r"^(?:unins\d{3}|uninstall.*|uninst)$", re.IGNORECASE)
_INNO_UNINSTALL_DATA = re.compile(r"unins\d{3}\.dat")


@dataclass(frozen=True, slots=True)
class InstallerPackage:
    """Evidence that a file is an installer package."""

    kind: str
    description: str


@dataclass(frozen=True, slots=True)
class InstalledProgram:
    """Folders and files Windows lists for one installed program."""

    name: str
    folders: tuple[str, ...]
    files: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _Section:
    name: bytes
    virtual_address: int
    virtual_size: int
    raw_offset: int
    raw_size: int


@dataclass(frozen=True, slots=True)
class _PeLayout:
    sections: tuple[_Section, ...]
    resource_rva: int
    image_end: int
    certificate: tuple[int, int]


def identify_installer_package(path: Path) -> InstallerPackage | None:
    """Return installer evidence from the file's own bytes, or ``None``.

    A few small reads at most (about 140 KB for an EXE); nothing is executed.
    """

    suffix = path.suffix.casefold()
    if suffix not in INSTALLER_PACKAGE_SUFFIXES and suffix != ".exe":
        return None
    try:
        with open(path, "rb") as handle:
            size = os.fstat(handle.fileno()).st_size
            if suffix == ".msi":
                if handle.read(len(_MSI_MAGIC)) == _MSI_MAGIC:
                    return InstallerPackage("msi", "Windows Installer 安装包")
                return None
            if suffix != ".exe":
                return _appx_evidence(handle, size)
            return _exe_evidence(handle, size)
    except (OSError, ValueError):
        return None


def _appx_evidence(handle: BinaryIO, size: int) -> InstallerPackage | None:
    head = _read_at(handle, 0, _ZIP_HEAD_BYTES)
    if not head.startswith(_ZIP_MAGIC):
        return None
    tail = _read_at(handle, max(0, size - _ZIP_TAIL_BYTES), _ZIP_TAIL_BYTES)
    if any(marker in head or marker in tail for marker in _APPX_MARKERS):
        return InstallerPackage("msix", "MSIX/APPX 应用安装包")
    return None


def _exe_evidence(handle: BinaryIO, size: int) -> InstallerPackage | None:
    layout = _pe_layout(handle, size)
    if layout is None:
        return None
    block = _version_block(handle, layout)
    strings = _version_strings(block)
    if any(_UNINSTALL_WORDS.search(value) for value in strings.values()):
        return None  # an uninstaller belongs to its program
    if any(section.name == b".wixburn" for section in layout.sections):
        return InstallerPackage("exe", "WiX 安装包")
    overlay = _overlay(handle, size, layout)
    for marker, description in _OVERLAY_MARKERS:
        if marker in overlay:
            return InstallerPackage("exe", description)
    text = block.decode("utf-16-le", "ignore").casefold()
    for name, description in _VERSION_MARKERS:
        if name.casefold() in text:
            return InstallerPackage("exe", description)
    for key in _VERSION_KEYS:
        value = strings.get(key, "")
        if _SETUP_WORDS.search(value):
            return InstallerPackage("exe", f"版本信息 {key} 为“{value}”的安装程序")
    return None


def _pe_layout(handle: BinaryIO, size: int) -> _PeLayout | None:
    dos = _read_at(handle, 0, 64)
    if len(dos) < 64 or dos[:2] != b"MZ":
        return None
    pe = _u32(dos, 60)
    if pe < 64 or pe + 24 > size:
        return None
    head = _read_at(handle, pe, 24 + 240)
    if len(head) < 24 or head[:4] != b"PE\0\0":
        return None
    count = _u16(head, 6)
    optional_size = _u16(head, 20)
    optional = head[24 : 24 + optional_size]
    directories = {0x10B: 96, 0x20B: 112}.get(_u16(optional, 0))
    if directories is None or not 0 < count <= _MAX_SECTIONS or len(optional) < directories:
        return None
    entries = _u32(optional, directories - 4)

    def directory(index: int) -> tuple[int, int]:
        at = directories + 8 * index
        if index >= entries or at + 8 > len(optional):
            return (0, 0)
        return (_u32(optional, at), _u32(optional, at + 4))

    table = _read_at(handle, pe + 24 + optional_size, 40 * count)
    if len(table) < 40 * count:
        return None
    sections = tuple(
        _Section(
            name=table[at : at + 8].rstrip(b"\0"),
            virtual_size=_u32(table, at + 8),
            virtual_address=_u32(table, at + 12),
            raw_size=_u32(table, at + 16),
            raw_offset=_u32(table, at + 20),
        )
        for at in range(0, 40 * count, 40)
    )
    image_end = max((item.raw_offset + item.raw_size for item in sections), default=0)
    return _PeLayout(sections, directory(2)[0], image_end, directory(4))


def _overlay(handle: BinaryIO, size: int, layout: _PeLayout) -> bytes:
    """The start of data appended after the image, skipping a signature there."""

    start = layout.image_end
    certificate_offset, certificate_size = layout.certificate
    if certificate_size and certificate_offset == start:
        start += certificate_size
    if not 0 < start < size:
        return b""
    return _read_at(handle, start, _OVERLAY_BYTES)


def _version_block(handle: BinaryIO, layout: _PeLayout) -> bytes:
    """The VS_VERSIONINFO resource: type 16, first name, first language."""

    root = _file_offset(layout, layout.resource_rva)
    if root is None:
        return b""
    node = _resource_child(handle, root, root, _RT_VERSION)
    for _level in ("name", "language"):
        if node is None or not node & _SUBDIRECTORY:
            return b""
        node = _resource_child(handle, root, root + (node & ~_SUBDIRECTORY), None)
    if node is None or node & _SUBDIRECTORY:
        return b""
    entry = _read_at(handle, root + node, 8)
    if len(entry) < 8:
        return b""
    offset = _file_offset(layout, _u32(entry, 0))
    if offset is None:
        return b""
    return _read_at(handle, offset, min(_u32(entry, 4), _VERSION_BYTES))


def _resource_child(
    handle: BinaryIO,
    root: int,
    directory: int,
    wanted: int | None,
) -> int | None:
    """Return the data offset of the wanted (or first) entry of a resource directory."""

    header = _read_at(handle, directory, 16)
    if len(header) < 16 or directory < root:
        return None
    count = _u16(header, 12) + _u16(header, 14)
    if count > _MAX_RESOURCE_ENTRIES:
        return None
    entries = _read_at(handle, directory + 16, 8 * count)
    for at in range(0, len(entries) - 7, 8):
        if wanted is None or _u32(entries, at) == wanted:
            return _u32(entries, at + 4)
    return None


def _file_offset(layout: _PeLayout, rva: int) -> int | None:
    if not rva:
        return None
    for section in layout.sections:
        delta = rva - section.virtual_address
        if 0 <= delta < max(section.virtual_size, section.raw_size):
            return section.raw_offset + delta if delta < section.raw_size else None
    return None


def _read_at(handle: BinaryIO, offset: int, length: int) -> bytes:
    if offset < 0 or length <= 0:
        return b""
    handle.seek(offset)
    return handle.read(length)


def _u16(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 2], "little")


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def looks_like_uninstaller(path: Path) -> bool:
    """Uninstallers belong to the program they remove (``unins000.exe`` and so on)."""

    return path.suffix.casefold() == ".exe" and bool(_UNINSTALLER_NAME.match(path.stem))


def has_installed_program_markers(folder: Path) -> bool:
    """An Inno Setup or NSIS uninstaller next to a file marks an installed program.

    Within one scan or cleanup run each folder is listed once: a folder holding
    thousands of downloads used to be listed again for every program in it.
    """

    if not in_scan_snapshot():
        return _folder_has_installed_program_markers(folder)
    known: dict[str, bool] = run_cached("installed_program_marker_folders", dict)
    key = os.path.normcase(os.path.abspath(folder))
    if key not in known:
        known[key] = _folder_has_installed_program_markers(folder)
    return known[key]


def _folder_has_installed_program_markers(folder: Path) -> bool:
    try:
        names = {entry.name.casefold() for entry in os.scandir(folder)}
    except OSError:
        return False
    if any(_INNO_UNINSTALL_DATA.fullmatch(name) for name in names):
        return True
    return bool(names & {"uninstall.exe", "uninst.exe"})


@snapshot_roots
def installed_programs(
    environment: Mapping[str, str] | None = None,
) -> tuple[InstalledProgram, ...]:
    """Programs Windows lists as installed, with folders too broad to trust removed."""

    if environment is not None:
        return ()
    broad = broad_folders()
    system = system_folders()
    programs: list[InstalledProgram] = []
    for entry in uninstall_entries():
        program = _installed_program(entry, broad, system)
        if program is not None:
            programs.append(program)
    return tuple(programs)


def installed_program_owner(path: Path) -> str | None:
    """Name the installed program that owns *path*, if any."""

    target = normalize_path(str(path))
    if target is None:
        return None
    for program in installed_programs():
        if target in program.files or any(is_within(target, folder) for folder in program.folders):
            return program.name
    return None


def program_location(path: Path) -> str | None:
    """Name the Windows or program-installation folder that holds *path*, if any."""

    target = normalize_path(str(path))
    if target is None:
        return None
    env = _environment()
    local = env.get("localappdata")
    locations = (
        (env.get("systemroot") or env.get("windir"), "Windows 系统文件夹"),
        (env.get("programfiles"), "Program Files 程序安装目录"),
        (env.get("programw6432"), "Program Files 程序安装目录"),
        (env.get("programfiles(x86)"), "Program Files (x86) 程序安装目录"),
        (local and os.path.join(local, "Programs"), "用户程序安装目录（LocalAppData\\Programs）"),
        (local and os.path.join(local, "Microsoft", "WindowsApps"), "Microsoft Store 应用目录"),
    )
    for folder, label in locations:
        root = normalize_path(folder) if folder else None
        if root is not None and is_within(target, root):
            return label
    return None


def system_installer_cache(path: Path) -> str | None:
    """Name the Windows installer cache that holds *path*, if any."""

    target = normalize_path(str(path))
    if target is None:
        return None
    env = _environment()
    caches = []
    system_root = env.get("systemroot") or env.get("windir")
    if system_root:
        caches.append((os.path.join(system_root, "Installer"), "Windows Installer 缓存"))
    program_data = env.get("programdata")
    if program_data:
        caches.append((os.path.join(program_data, "Package Cache"), "安装包缓存（Package Cache）"))
    for folder, label in caches:
        root = normalize_path(folder)
        if root is not None and is_within(target, root):
            return label
    return None


def broad_folders() -> frozenset[str]:
    """Folders that hold many programs or user files; never one program's folder."""

    env = _environment()
    candidates: list[str] = []
    profile = env.get("userprofile")
    if profile:
        candidates.append(profile)
        candidates.extend(
            os.path.join(profile, name) for name in ("Downloads", "Desktop", "Documents")
        )
    for key in (
        "appdata",
        "localappdata",
        "programdata",
        "programfiles",
        "programfiles(x86)",
        "programw6432",
        "systemroot",
        "windir",
        "temp",
        "tmp",
    ):
        if env.get(key):
            candidates.append(env[key])
    if env.get("localappdata"):
        candidates.append(os.path.join(env["localappdata"], "Programs"))
    folders = {normalize_path(candidate) for candidate in candidates}
    folders.update(normalize_path(Path(candidate).anchor) for candidate in candidates)
    return frozenset(folder for folder in folders if folder)


def system_folders() -> tuple[str, ...]:
    """The Windows folder; programs there (msiexec, rundll32) belong to Windows."""

    env = _environment()
    roots = (normalize_path(root) for root in (env.get("systemroot"), env.get("windir")) if root)
    return tuple(dict.fromkeys(root for root in roots if root))


def is_within(target: str, folder: str) -> bool:
    """Whether normalised *target* lies strictly inside normalised *folder*."""

    return target.startswith(folder.rstrip(os.sep) + os.sep)


def _installed_program(
    entry: UninstallEntry,
    broad: frozenset[str],
    system: tuple[str, ...],
) -> InstalledProgram | None:
    """Folders and files one Uninstall entry names; its commands are parsed, never run."""

    def usable(folder: str) -> bool:
        return folder not in broad and not any(
            folder == root or is_within(folder, root) for root in system
        )

    folders: list[str] = []
    files: list[str] = []
    location = normalize_path(entry.install_location)
    if location is not None and usable(location):
        folders.append(location)
    for command in (entry.uninstall_string, entry.display_icon):
        # msiexec.exe, rundll32.exe and broad folders such as Downloads are skipped.
        program, named_folders, named_files = program_and_paths(command, usable=usable)
        folders.extend(named_folders)
        files.extend(named_files)
        if program is not None:
            files.append(program)
            if usable(os.path.dirname(program)):
                folders.append(os.path.dirname(program))
    if not folders and not files:
        return None
    return InstalledProgram(
        entry.display_name,
        tuple(dict.fromkeys(folders)),
        tuple(dict.fromkeys(files)),
    )


def _environment() -> dict[str, str]:
    return {key.casefold(): value for key, value in os.environ.items() if value}


def _version_strings(head: bytes) -> dict[str, str]:
    """Read selected VS_VERSIONINFO strings from a version block, best effort."""

    found: dict[str, str] = {}
    for key in _VERSION_KEYS:
        needle = key.encode("utf-16-le") + b"\x00\x00"
        index = head.find(needle)
        if index < 0:
            continue
        start = index + len(needle)
        start += (-start) % 4  # string values are DWORD aligned
        end = start
        while end + 1 < len(head) and end - start < 512 and head[end : end + 2] != b"\x00\x00":
            end += 2
        try:
            value = head[start:end].decode("utf-16-le").strip()
        except UnicodeDecodeError:
            continue
        if value:
            found[key] = value
    return found


__all__ = [
    "INSTALLER_PACKAGE_SUFFIXES",
    "InstalledProgram",
    "InstallerPackage",
    "broad_folders",
    "has_installed_program_markers",
    "identify_installer_package",
    "installed_program_owner",
    "installed_programs",
    "is_within",
    "looks_like_uninstaller",
    "program_location",
    "system_folders",
    "system_installer_cache",
]
