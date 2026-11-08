"""What an installer-looking file is, and who owns it (installer_files)."""

# Expected UI strings are Chinese prose with fullwidth punctuation.
# ruff: noqa: RUF001

from __future__ import annotations

import io
import os
import re
import struct
import winreg
import zipfile
from pathlib import Path
from typing import Any

import pytest

import devclean.core.installer_files as installer_files
from devclean.core.installer_files import (
    has_installed_program_markers,
    identify_installer_package,
    installed_program_owner,
    looks_like_uninstaller,
    system_installer_cache,
)
from devclean.platform.windows.registry import UninstallEntry, uninstall_entries
from tests import pe_samples

REPO_ROOT = Path(__file__).resolve().parents[1]


def _file(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _kind(tmp_path: Path, name: str, data: bytes) -> str | None:
    package = identify_installer_package(_file(tmp_path, name, data))
    return None if package is None else package.description


# ---- evidence from the file's own structure ----------------------------------------


def test_installer_frameworks_are_recognised_from_their_structure(tmp_path: Path) -> None:
    seven_zip = pe_samples.build_pe(overlay=b';!@Install@!UTF-8!\r\nTitle="Tool"\r\n')
    cases = {
        "nsis.exe": (pe_samples.nsis_installer(), "NSIS 安装程序"),
        "inno.exe": (pe_samples.inno_installer(), "Inno Setup 安装程序"),
        "wix.exe": (pe_samples.build_pe(extra_sections=(b".wixburn",)), "WiX 安装包"),
        "sfx.exe": (seven_zip, "7-Zip 自解压安装程序"),
        "chrome.exe": (
            pe_samples.versioned_program("Google Chrome Installer"),
            "版本信息 FileDescription 为“Google Chrome Installer”的安装程序",
        ),
    }
    for name, (data, description) in cases.items():
        assert _kind(tmp_path, name, data) == description, name


def test_signed_installer_is_still_recognised(tmp_path: Path) -> None:
    signature = b"\x00" * 8 + b"PKCS7 signature bytes"
    nsis = pe_samples.build_pe(
        overlay=b"\0" * 8 + b"NullsoftInst" + b"\0" * 64,
        signature=signature,
    )

    assert _kind(tmp_path, "signed-setup.exe", nsis) == "NSIS 安装程序"


def test_programs_that_merely_mention_an_installer_are_not_installers(tmp_path: Path) -> None:
    cases = {
        # The NSIS compiler and 7-Zip carry these strings in their own code.
        "makensis.exe": pe_samples.build_pe(code=b"\0" * 64 + b"NullsoftInst" + b"\0" * 64),
        "7zFM.exe": pe_samples.build_pe(code=b";!@Install@!UTF-8!"),
        # "Setup" inside a longer word is a tool name, not a setup program.
        "WinSetupFromUSB.exe": pe_samples.versioned_program("WinSetupFromUSB"),
        # Inno Setup's uninstaller has the same version text as its setup.
        "Inno-uninstaller.exe": pe_samples.build_pe(
            version={
                "Comments": "This installation was built with Inno Setup.",
                "FileDescription": "Setup/Uninstall",
            }
        ),
        "portable.exe": pe_samples.portable_program(),
        # Signed, with the signature right after the image and nothing else.
        "signed-tool.exe": pe_samples.build_pe(signature=b"\0" * 8 + b"NullsoftInst"),
    }
    for name, data in cases.items():
        assert _kind(tmp_path, name, data) is None, name


def test_malformed_executables_are_not_installers_and_do_not_raise(tmp_path: Path) -> None:
    valid = pe_samples.nsis_installer()
    pe_offset = struct.unpack_from("<I", valid, 60)[0]
    huge_section_count = bytearray(valid)
    struct.pack_into("<H", huge_section_count, pe_offset + 6, 0xFFFF)
    cases = {
        "empty.exe": b"",
        "mz-only.exe": b"MZ",
        "random.exe": b"MZ" + os.urandom(4096),
        "pe-beyond-eof.exe": b"MZ".ljust(60, b"\0") + struct.pack("<I", 0x7FFFFFF0),
        "truncated.exe": valid[: pe_offset + 30],
        "sections.exe": bytes(huge_section_count),
        "not-pe.exe": b"#!/bin/sh\nNullsoftInst\n",
    }
    for name, data in cases.items():
        assert _kind(tmp_path, name, data) is None, name


def test_package_files_need_their_format_not_just_their_name(tmp_path: Path) -> None:
    msix = tmp_path / "app.msix"
    with zipfile.ZipFile(msix, "w") as archive:
        archive.writestr("AppxManifest.xml", "<Package/>")
    plain_zip = tmp_path / "renamed.msix"
    with zipfile.ZipFile(plain_zip, "w") as archive:
        archive.writestr("readme.txt", "hello")

    msi = identify_installer_package(
        _file(tmp_path, "tool.msi", bytes.fromhex("D0CF11E0A1B11AE1") + b"\0" * 504)
    )
    assert msi is not None and msi.kind == "msi"
    msix_package = identify_installer_package(msix)
    assert msix_package is not None and msix_package.kind == "msix"
    assert identify_installer_package(plain_zip) is None
    assert identify_installer_package(_file(tmp_path, "fake.msi", b"<html>")) is None
    assert identify_installer_package(_file(tmp_path, "notes.txt", b"setup")) is None


def test_real_portable_devclean_executable_is_not_an_installer() -> None:
    executable = REPO_ROOT / "release" / "DevClean.exe"
    if not executable.is_file():
        pytest.skip("release/DevClean.exe is not in this checkout")

    assert identify_installer_package(executable) is None


def test_detection_reads_a_few_small_ranges_of_a_large_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = pe_samples.build_pe(
        code=b"\x90" * (8 * 1024 * 1024),
        version={"FileDescription": "Big Tool"},
        overlay=b"\0" * 8 + b"NullsoftInst" + b"\0" * (4 * 1024 * 1024),
    )
    path = _file(tmp_path, "big-setup.exe", big)
    read_bytes: list[int] = []

    class CountingFile(io.FileIO):
        def read(self, size: int | None = -1, /) -> bytes:
            data = super().read(size)
            read_bytes.append(len(data))
            return data

    def counting_open(file: Any, mode: str = "r", *_args: Any, **_kwargs: Any) -> Any:
        assert mode == "rb"
        return CountingFile(os.fspath(file), "r")

    monkeypatch.setattr(installer_files, "open", counting_open, raising=False)
    package = identify_installer_package(path)

    assert package is not None and package.description == "NSIS 安装程序"
    assert sum(read_bytes) <= 160 * 1024
    assert len(read_bytes) <= 16


# ---- names and neighbours that mark an installed program ---------------------------


def test_uninstallers_and_installed_program_folders_are_recognised(tmp_path: Path) -> None:
    for name in ("unins000.exe", "Uninstall.exe", "uninstall Foo.exe", "uninst.exe"):
        assert looks_like_uninstaller(Path(name)), name
    for name in ("setup.exe", "uninstall.txt", "installer.exe"):
        assert not looks_like_uninstaller(Path(name)), name

    inno = tmp_path / "inno"
    inno.mkdir()
    (inno / "unins000.dat").write_bytes(b"log")
    nsis = tmp_path / "nsis"
    nsis.mkdir()
    (nsis / "Uninstall.exe").write_bytes(b"MZ")
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "setup.exe").write_bytes(b"MZ")

    assert has_installed_program_markers(inno)
    assert has_installed_program_markers(nsis)
    assert not has_installed_program_markers(plain)
    assert not has_installed_program_markers(tmp_path / "missing")


# ---- Windows' installed-programs list ------------------------------------------------


@pytest.fixture
def profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    for name, value in {
        "USERPROFILE": home,
        "APPDATA": home / "AppData" / "Roaming",
        "LOCALAPPDATA": home / "AppData" / "Local",
        "TEMP": home / "AppData" / "Local" / "Temp",
        "TMP": home / "AppData" / "Local" / "Temp",
        "PROGRAMDATA": tmp_path / "ProgramData",
        "PROGRAMFILES": tmp_path / "Program Files",
        "SYSTEMROOT": tmp_path / "Windows",
    }.items():
        monkeypatch.setenv(name, str(value))
    return home


def test_installed_programs_own_their_folder_uninstaller_and_icon(
    profile: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloads = profile / "Downloads"
    system32 = tmp_path / "Windows" / "System32"
    foo = tmp_path / "Apps" / "Foo"
    bar = profile / "Tools" / "Bar"
    baz = tmp_path / "Apps" / "Baz"
    entries = (
        UninstallEntry("Foo", f'"{foo}"', "", ""),
        UninstallEntry("Bar", "", f'"{bar / "unins000.exe"}" /SILENT', ""),
        UninstallEntry("Baz", "", "", f"{baz / 'baz.exe'},0"),
        # Too broad to mean one program: a Downloads install location.
        UninstallEntry("Sloppy", str(downloads), "", ""),
        # Windows Installer's own uninstall command names no program file.
        UninstallEntry("MsiApp", "", "MsiExec.exe /X{0000}", ""),
        UninstallEntry("MsiApp2", "", f"{system32 / 'msiexec.exe'} /X", ""),
    )
    monkeypatch.setattr(installer_files, "uninstall_entries", lambda: entries)

    assert installed_program_owner(foo / "data" / "setup.msi") == "Foo"
    assert installed_program_owner(bar / "unins000.exe") == "Bar"
    assert installed_program_owner(bar / "bar-setup.exe") == "Bar"
    assert installed_program_owner(baz / "baz.exe") == "Baz"
    assert installed_program_owner(downloads / "tool-setup.exe") is None
    assert installed_program_owner(tmp_path / "Windows" / "System32" / "x.exe") is None
    assert installed_program_owner(tmp_path / "Apps" / "Other" / "setup.exe") is None


def test_windows_installer_caches_are_named(profile: Path, tmp_path: Path) -> None:
    windows_installer = tmp_path / "Windows" / "Installer" / "1a2b.msi"
    package_cache = tmp_path / "ProgramData" / "Package Cache" / "{id}" / "vc_redist.exe"

    assert system_installer_cache(windows_installer) == "Windows Installer 缓存"
    assert system_installer_cache(package_cache) == "安装包缓存（Package Cache）"
    assert system_installer_cache(profile / "Downloads" / "Installer" / "a.msi") is None


def test_uninstall_entries_read_every_registry_view(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hklm64 = (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY)
    hkcu = (winreg.HKEY_CURRENT_USER, 0)
    hives: dict[tuple[int, int], dict[str, dict[str, tuple[object, int]]]] = {
        hklm64: {
            "{GUID-FOO}": {
                "DisplayName": ("Foo", winreg.REG_SZ),
                "InstallLocation": ("/apps/Foo", winreg.REG_SZ),
                "EstimatedSize": (100, winreg.REG_DWORD),
            },
            "NoName": {"UninstallString": ("/apps/NoName/uninstall.exe", winreg.REG_SZ)},
        },
        hkcu: {
            "Bar": {
                "DisplayName": ("Bar", winreg.REG_SZ),
                "DisplayIcon": ("%BAR_HOME%/bar.exe,0", winreg.REG_EXPAND_SZ),
            },
        },
    }
    monkeypatch.setenv("BAR_HOME", "/home/user/Bar")

    def open_key(parent: Any, name: str, _reserved: int = 0, access: int = 0) -> Any:
        if isinstance(parent, tuple):
            return ("key", parent[1][name])
        view = (parent, access & (winreg.KEY_WOW64_64KEY | winreg.KEY_WOW64_32KEY))
        assert name == r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        if view not in hives:
            raise FileNotFoundError(name)  # e.g. no 32-bit view
        return ("parent", hives[view])

    def enum_key(parent: Any, index: int) -> str:
        names = [str(name) for name in parent[1]]
        if index >= len(names):
            raise OSError("no more keys")
        return names[index]

    def query_value(key: Any, name: str) -> tuple[object, int]:
        if name not in key[1]:
            raise FileNotFoundError(name)
        values: dict[str, tuple[object, int]] = key[1]
        return values[name]

    def expand(text: str) -> str:
        return re.sub(r"%(\w+)%", lambda match: os.environ.get(match.group(1), ""), text)

    monkeypatch.setattr(winreg, "OpenKey", open_key)
    monkeypatch.setattr(winreg, "EnumKey", enum_key)
    monkeypatch.setattr(winreg, "QueryValueEx", query_value)
    monkeypatch.setattr(winreg, "CloseKey", lambda _key: None)
    monkeypatch.setattr(winreg, "ExpandEnvironmentStrings", expand, raising=False)

    assert uninstall_entries() == (
        UninstallEntry("Foo", "/apps/Foo", "", ""),
        UninstallEntry("NoName", "", "/apps/NoName/uninstall.exe", ""),
        UninstallEntry("Bar", "", "", "/home/user/Bar/bar.exe,0"),
    )
