"""Resolve external programs to absolute paths before they are started.

Given a bare program name, Windows ``CreateProcess`` searches the folder the
running EXE was loaded from and the current directory before System32 and
PATH, and ``shutil.which`` also tries the current directory first. DevClean.exe
is usually run straight from Downloads, so a bare name could start any file
with that name sitting next to it. External programs are therefore started by
absolute path only: Windows' own tools from System32, everything else from
absolute PATH entries. A program that cannot be found that way is treated as
not installed.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import PureWindowsPath

_DEFAULT_PATHEXT = ".COM;.EXE;.BAT;.CMD"

# Windows components that live in System32, keyed by name without ".exe".
# They are never looked up by name.
_SYSTEM32_TOOLS = {
    "powershell": r"WindowsPowerShell\v1.0\powershell.exe",
    "tasklist": "tasklist.exe",
    "cmd": "cmd.exe",
    "wsl": "wsl.exe",
    "dism": "dism.exe",
    "cleanmgr": "cleanmgr.exe",
}


def system_tool(relative: str, environment: Mapping[str, str] | None = None) -> str:
    """Return the absolute path of a tool shipped in ``%SystemRoot%\\System32``."""

    env = _casefold(environment)
    root = env.get("systemroot") or env.get("windir") or ""
    if not PureWindowsPath(root).is_absolute():
        root = r"C:\Windows"
    return str(PureWindowsPath(root) / "System32" / relative)


def path_tool(
    name: str,
    environment: Mapping[str, str] | None = None,
    *,
    search_path: str | None = None,
) -> str | None:
    """Find *name* in absolute PATH entries only; ``None`` means not installed.

    An explicit location is accepted only when it is an absolute path. Relative
    PATH entries are skipped, and neither the current directory nor DevClean's
    own folder is searched unless PATH lists it explicitly by absolute path.
    *search_path* replaces the environment's PATH when given.
    """

    if not name:
        return None
    if _has_directory(name):
        return name if os.path.isabs(name) else None
    env = _casefold(environment)
    names = [name]
    if os.name == "nt" and not os.path.splitext(name)[1]:
        extensions = (env.get("pathext") or _DEFAULT_PATHEXT).split(";")
        names = [name + extension.lower() for extension in extensions if extension]
    entries = search_path if search_path is not None else env.get("path") or ""
    for entry in entries.split(os.pathsep):
        directory = entry.strip().strip('"')
        if not directory or not os.path.isabs(directory):
            continue
        for candidate_name in names:
            candidate = os.path.join(directory, candidate_name)
            if os.path.isfile(candidate) and (os.name == "nt" or os.access(candidate, os.X_OK)):
                return candidate
    return None


def resolve_program(program: str, environment: Mapping[str, str] | None = None) -> str:
    """Return an absolute path for *program* or raise ``FileNotFoundError``."""

    if _has_directory(program):
        if os.path.isabs(program):
            return program
        raise FileNotFoundError(f"refusing to start a relative program path: {program}")
    system = _SYSTEM32_TOOLS.get(program.casefold().removesuffix(".exe"))
    if system is not None:
        return system_tool(system, environment)
    resolved = path_tool(program, environment)
    if resolved is None:
        raise FileNotFoundError(f"{program} was not found in an absolute PATH entry")
    return resolved


def resolve_command(args: Sequence[str | os.PathLike[str]]) -> list[str]:
    """Return *args* with the program name replaced by its absolute path."""

    if not args:
        raise ValueError("empty command")
    program, *rest = (os.fspath(part) for part in args)
    return [resolve_program(str(program)), *(str(part) for part in rest)]


def _has_directory(name: str) -> bool:
    return any(separator in name for separator in ("\\", "/")) or ":" in name


def _casefold(environment: Mapping[str, str] | None) -> dict[str, str]:
    source = os.environ if environment is None else environment
    return {key.casefold(): value for key, value in source.items() if value}


__all__ = ["path_tool", "resolve_command", "resolve_program", "system_tool"]
