"""Paths in Windows command lines and registry strings (command_paths, live owners)."""

from __future__ import annotations

import ntpath
import os
import re
import subprocess
import winreg
from pathlib import Path
from typing import Any

import pytest

import devclean.platform.windows.command_paths as command_paths
from devclean.core import live_owners
from devclean.core.application_cleanup import scan_root_snapshot
from devclean.platform.windows.command_paths import (
    command_paths as paths_in,
)
from devclean.platform.windows.command_paths import (
    normalize_path,
    program_and_paths,
)
from devclean.platform.windows.processes import (
    ProcessInfo,
    ProcessListError,
    parse_process_list,
)
from devclean.platform.windows.registry import ServiceEntry, service_entries

ENV = {"ProgramFiles": r"C:\Program Files", "LOCALAPPDATA": r"C:\Users\Ann\AppData\Local"}


def _win(command: str) -> tuple[str, ...]:
    return paths_in(command, ENV, pathmod=ntpath)


# ---- Uninstall strings and other Windows command lines ------------------------------


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # quoted program with spaces and switches
        (r'"C:\Program Files\Foo App\uninst.exe" /S', [r"c:\program files\foo app\uninst.exe"]),
        # unquoted program with spaces: the first prefix ending in .exe
        (
            r"C:\Program Files\Bar Tool\uninstall.exe /S /D=C:\Bar Data",
            [r"c:\program files\bar tool\uninstall.exe", r"c:\bar"],
        ),
        # %VARIABLES% in a plain (REG_SZ) string
        (r"%ProgramFiles%\Baz\uninstall.exe", [r"c:\program files\baz\uninstall.exe"]),
        # rundll32 "dll",Entry: rundll32 names no location, the DLL does
        (
            r'rundll32.exe "C:\Program Files\Qux\qux.dll",Uninstall',
            [r"c:\program files\qux\qux.dll"],
        ),
        # icon index, quoted and unquoted
        (r'"C:\Apps\Icon\app.exe",0', [r"c:\apps\icon\app.exe"]),
        (r"C:\Apps\Icon\app.exe,-101", [r"c:\apps\icon\app.exe"]),
        # extended-length prefixes
        (r"\\?\C:\Apps\Long\uninst.exe", [r"c:\apps\long\uninst.exe"]),
        (r"\\?\UNC\server\share\uninst.exe", [r"\\server\share\uninst.exe"]),
        # cmd /c with nested quotes
        (
            r'cmd.exe /c ""C:\Program Files\Foo\uninstall.bat" /S"',
            [r"c:\program files\foo\uninstall.bat"],
        ),
        # NSIS _?= install folder and name=value / switch:value arguments
        (
            r'"C:\Temp2\Un_A.exe" _?=C:\Apps\NsisDir',
            [r"c:\temp2\un_a.exe", r"c:\apps\nsisdir"],
        ),
        (r"java.exe -Dmaven.repo.local=D:\m2 -jar x.jar", [r"d:\m2"]),
        (r'Code.exe --user-data-dir="D:\State With Space"', [r"d:\state with space"]),
        (r"tool.exe /LOG:D:\logs\tool.log", [r"d:\logs\tool.log"]),
        # Windows Installer: no file location at all
        (r"MsiExec.exe /X{12345678-ABCD-1234-ABCD-1234}", []),
        ("", []),
    ],
)
def test_command_lines_name_their_locations(command: str, expected: list[str]) -> None:
    assert list(_win(command)) == expected


def test_short_8_3_names_become_long_names(monkeypatch: pytest.MonkeyPatch) -> None:
    long_names = {"PROGRA~1": "Program Files", "FOOAPP~1": "Foo App", "UNINST~1.EXE": "uninst.exe"}

    def fake_long_path(path: str) -> str:
        return re.sub(r"[^\\]+~\d[^\\]*", lambda match: long_names[match.group(0)], path)

    monkeypatch.setattr(command_paths, "long_path", fake_long_path)

    assert _win(r"C:\PROGRA~1\FOOAPP~1\UNINST~1.EXE /S") == (
        r"c:\program files\foo app\uninst.exe",
    )
    assert normalize_path(r'"C:\PROGRA~1\FOOAPP~1"', pathmod=ntpath) == r"c:\program files\foo app"


def test_program_owns_its_folder_but_a_log_file_argument_does_not(tmp_path: Path) -> None:
    program_folder = tmp_path / "Foo App"
    data = tmp_path / "Foo Data"
    data.mkdir()
    command = f'"{program_folder / "uninst.exe"}" /D="{data}" /LOG="{tmp_path / "x.log"}"'

    program, folders, files = program_and_paths(command, usable=lambda _path: True)

    assert program == normalize_path(str(program_folder / "uninst.exe"))
    assert folders == (normalize_path(str(data)),)
    assert files == (normalize_path(str(tmp_path / "x.log")),)


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist only on Windows")
def test_long_path_expands_real_windows_short_names(tmp_path: Path) -> None:
    import ctypes

    folder = tmp_path / "A Long Folder Name For Testing"
    folder.mkdir()
    buffer = ctypes.create_unicode_buffer(512)
    if not ctypes.windll.kernel32.GetShortPathNameW(str(folder), buffer, 512):
        pytest.skip("short names unavailable")
    short = buffer.value
    if "~" not in short:
        pytest.skip("8.3 name generation is disabled on this volume")

    assert os.path.normcase(command_paths.long_path(short)) == os.path.normcase(str(folder))
    missing = short + "\\not-created.exe"
    assert os.path.normcase(command_paths.long_path(missing)) == os.path.normcase(
        str(folder / "not-created.exe")
    )


def test_parsing_never_starts_the_named_program(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = tmp_path / "ran"
    program = tmp_path / "Foo App" / "uninst.exe"
    program.parent.mkdir()
    program.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    program.chmod(0o755)
    started: list[object] = []

    def refuse(*args: object, **_kwargs: object) -> None:
        started.append(args)
        raise AssertionError("parsing must not start programs")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "system", refuse)
    for command in (f'"{program}" /S', f"{program} /S", f'cmd.exe /c ""{program}" /S"'):
        assert normalize_path(str(program)) in paths_in(command)

    assert started == [] and not marker.exists()


# ---- the process list -----------------------------------------------------------------


def test_process_list_is_parsed_from_one_query() -> None:
    many = (
        '[{"Name":"System","ExecutablePath":null,"CommandLine":null},'
        '{"Name":"Code.exe","ExecutablePath":"C:\\\\VS Code\\\\Code.exe",'
        '"CommandLine":"\\"C:\\\\VS Code\\\\Code.exe\\" D:\\\\proj"}]'
    )
    assert parse_process_list(many) == (
        ProcessInfo("System", "", ""),
        ProcessInfo("Code.exe", r"C:\VS Code\Code.exe", r'"C:\VS Code\Code.exe" D:\proj'),
    )
    one = '{"Name":"x.exe","ExecutablePath":"C:\\\\x.exe","CommandLine":"x.exe"}'
    assert parse_process_list(one) == (ProcessInfo("x.exe", r"C:\x.exe", "x.exe"),)
    for broken in ("", "not json", "[]", '[{"ExecutablePath":"C:\\\\x.exe"}]'):
        with pytest.raises(ProcessListError):
            parse_process_list(broken)


def test_failed_process_query_is_an_error_not_an_empty_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import devclean.platform.windows.processes as processes

    monkeypatch.setattr(processes, "os", type("WindowsOs", (), {"name": "nt"}))

    def fail(*_args: object, **_kwargs: object) -> Any:
        raise OSError("powershell missing")

    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(ProcessListError, match="could not run"):
        processes.running_processes()

    def exit_one(*_args: object, **_kwargs: object) -> Any:
        return subprocess.CompletedProcess(["powershell.exe"], 1, "", "boom")

    monkeypatch.setattr(subprocess, "run", exit_one)
    with pytest.raises(ProcessListError, match="exit code 1"):
        processes.running_processes()


# ---- what the pre-delete check refuses ------------------------------------------------


def test_directory_is_refused_when_something_running_uses_a_path_inside_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "proj"
    (project / "out").mkdir(parents=True)
    other = tmp_path / "old-build"
    other.mkdir()
    server = ProcessInfo(
        "server.exe",
        str(tmp_path / "bin" / "server.exe"),
        f'"{tmp_path / "bin" / "server.exe"}" --root "{project / "out"}"',
    )
    monkeypatch.setattr(live_owners, "list_processes", lambda: (server,))

    with scan_root_snapshot():
        assert live_owners.live_owner_refusal(project, directory=True) is not None
        assert live_owners.live_owner_refusal(project / "out" / "a.txt") is not None
        assert live_owners.live_owner_refusal(project / "readme.txt") is None
        assert live_owners.live_owner_refusal(other, directory=True) is None
        # The program's own folder belongs to it as well.
        assert live_owners.live_owner_refusal(tmp_path / "bin" / "server.log") is not None


def test_broad_and_windows_folders_named_by_processes_do_not_block_everything(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    downloads = home / "Downloads"
    downloads.mkdir(parents=True)
    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SYSTEMROOT", str(windows))
    explorer = ProcessInfo(
        "explorer.exe", str(windows / "explorer.exe"), f'"{windows / "explorer.exe"}" "{downloads}"'
    )
    viewer = ProcessInfo(
        "viewer.exe",
        str(tmp_path / "Viewer" / "viewer.exe"),
        f'"{tmp_path / "Viewer" / "viewer.exe"}" "{downloads / "doc.pdf"}"',
    )
    monkeypatch.setattr(live_owners, "list_processes", lambda: (explorer, viewer))

    with scan_root_snapshot():
        assert live_owners.live_owner_refusal(downloads / "old-setup.exe") is None
        assert live_owners.live_owner_refusal(downloads / "doc.pdf") is not None


def test_service_settings_are_read_from_the_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    multi = getattr(winreg, "REG_MULTI_SZ", 7)
    services: dict[str, dict[str, Any] | None] = {
        "ModelSvc": {
            "values": [
                ("ImagePath", r'"C:\Svc\model.exe" serve', winreg.REG_SZ),
                ("Environment", ["MODELS=D:\\models", "LOG=info"], multi),
                ("Start", 2, winreg.REG_DWORD),
            ],
            "Parameters": [
                ("AppDirectory", "%DATA_ROOT%\\svc", winreg.REG_EXPAND_SZ),
                ("AppParameters", "--port 1", winreg.REG_SZ),
            ],
        },
        "Locked": None,  # a service key that cannot be opened is skipped
        "Driver": {"values": [("ImagePath", r"\SystemRoot\System32\drivers\x.sys", 2)]},
    }
    monkeypatch.setenv("DATA_ROOT", "E:\\data")

    def open_key(parent: Any, name: str, _reserved: int = 0, _access: int = 0) -> Any:
        if parent == winreg.HKEY_LOCAL_MACHINE:
            assert name == r"SYSTEM\CurrentControlSet\Services"
            return ("root", list(services))
        if parent[0] == "root":
            service = services[name]
            if service is None:
                raise PermissionError(name)
            return ("service", service)
        if name == "Parameters" and "Parameters" in parent[1]:
            return ("parameters", {"values": parent[1]["Parameters"]})
        raise FileNotFoundError(name)

    def enum_key(key: Any, index: int) -> str:
        names: list[str] = key[1]
        if index >= len(names):
            raise OSError("no more")
        return names[index]

    def enum_value(key: Any, index: int) -> tuple[str, object, int]:
        values: list[tuple[str, object, int]] = key[1]["values"]
        if index >= len(values):
            raise OSError("no more")
        return values[index]

    def query_value(key: Any, name: str) -> tuple[object, int]:
        for value_name, data, kind in key[1]["values"]:
            if value_name == name:
                return data, kind
        raise FileNotFoundError(name)

    def expand(text: str) -> str:
        return re.sub(r"%(\w+)%", lambda match: os.environ.get(match.group(1), ""), text)

    for name, function in {
        "OpenKey": open_key,
        "EnumKey": enum_key,
        "EnumValue": enum_value,
        "QueryValueEx": query_value,
        "CloseKey": lambda _key: None,
        "ExpandEnvironmentStrings": expand,
    }.items():
        monkeypatch.setattr(winreg, name, function, raising=False)
    monkeypatch.setattr(winreg, "REG_MULTI_SZ", multi, raising=False)

    assert service_entries() == (
        ServiceEntry(
            "ModelSvc",
            r'"C:\Svc\model.exe" serve',
            ("MODELS=D:\\models", "LOG=info", "E:\\data\\svc", "--port 1"),
        ),
        ServiceEntry("Driver", r"\SystemRoot\System32\drivers\x.sys", ()),
    )
    # A driver's "\SystemRoot\..." image path names no ordinary file location.
    assert _win(r"\SystemRoot\System32\drivers\x.sys") == ()


# ---- hidden command lines: judged per target ---------------------------------------


def test_hidden_programs_guard_related_directories_and_installer_packages(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "worker.exe").write_bytes(b"MZ")
    other = tmp_path / "other-cache"
    other.mkdir()
    package = tmp_path / "pkgs" / "tool.msi"
    hidden = (
        ProcessInfo("worker.exe", "", "", session_id=1),
        ProcessInfo("agentx.exe", str(tmp_path / "agentx" / "agentx.exe"), "", session_id=1),
        ProcessInfo("msiexec.exe", "", "", session_id=0),
    )
    monkeypatch.setattr(live_owners, "list_processes", lambda: hidden)

    with scan_root_snapshot():
        refusal = live_owners.live_owner_refusal
        # a program of that name inside the directory being deleted
        assert refusal(cache, directory=True, rule_proven=True) is not None
        # the directory that holds a hidden program's visible program file
        assert refusal(tmp_path / "agentx", directory=True, rule_proven=True) is not None
        # Windows Installer is running: installer packages wait
        assert refusal(package, rule_proven=True) is not None
        # unrelated rule-proven directory and file are fine
        assert refusal(other, directory=True, rule_proven=True) is None
        assert refusal(tmp_path / "pkgs" / "tool.zip", rule_proven=True) is None
        # an unrelated reviewed item is not
        assert refusal(tmp_path / "pkgs" / "tool.zip") is not None


def test_process_session_is_read_when_windows_gives_it() -> None:
    rows = (
        '[{"Name":"a.exe","SessionId":0,"ExecutablePath":null,"CommandLine":null},'
        '{"Name":"b.exe","SessionId":2,"ExecutablePath":null,"CommandLine":null},'
        '{"Name":"c.exe","SessionId":null,"ExecutablePath":null,"CommandLine":null},'
        '{"Name":"d.exe","SessionId":true,"ExecutablePath":null,"CommandLine":null}]'
    )
    assert [process.session_id for process in parse_process_list(rows)] == [0, 2, None, None]


def test_directory_that_cannot_be_fully_searched_is_refused_with_the_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    big = tmp_path / "big-cache"
    big.mkdir()
    for index in range(5):
        (big / f"f{index}.bin").write_bytes(b"x")
    locked = tmp_path / "locked-cache"
    (locked / "inner").mkdir(parents=True)
    hidden = (ProcessInfo("worker.exe", "", "", session_id=1),)
    monkeypatch.setattr(live_owners, "list_processes", lambda: hidden)
    monkeypatch.setattr(live_owners, "_TREE_LIMIT", 3)

    def unreadable(path: object) -> Any:
        if Path(str(path)) == locked / "inner":
            raise PermissionError(str(path))
        return real_scandir(path)

    real_scandir = os.scandir
    monkeypatch.setattr(os, "scandir", unreadable)
    with scan_root_snapshot():
        for directory in (big, locked):
            refusal = live_owners.live_owner_refusal(directory, directory=True, rule_proven=True)
            assert refusal is not None and "无法读取" in refusal, directory
