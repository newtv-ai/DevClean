"""Paths named in Windows command lines and registry values, read conservatively.

Used to work out which files and folders an installed program, a running
process or a service uses. Commands are only parsed here; nothing is started.

Windows writes the same location in many shapes: quoted or not, with spaces,
with ``%VARIABLES%``, with an ``\\\\?\\`` prefix, with 8.3 short names such as
``C:\\PROGRA~1``, or with a trailing ``,0`` icon index or ``,EntryPoint``. Every
shape is reduced to one normalised long path so it can be compared with the
paths a scan sees.
"""

from __future__ import annotations

import ctypes
import os
import re
from collections.abc import Callable, Mapping
from ctypes import wintypes
from types import ModuleType

_PROGRAM_SUFFIXES = (".exe", ".com", ".bat", ".cmd")
_EXTENDED_UNC = "\\\\?\\UNC\\"
_EXTENDED = "\\\\?\\"
_QUOTED = re.compile(r'"([^"]*)"')
_VARIABLE = re.compile(r"%([^%\s]+)%")
# "C:\Foo\app.exe,0" (icon index) or "C:\Foo\setup.dll,Uninstall" (rundll32 entry).
_TRAILING_ENTRY = re.compile(r"^(.+\.[A-Za-z0-9]{1,4}),\s*(?:-?\d+|[A-Za-z_][\w]*)$")
# "/LOG=C:\x", "--data-dir=D:\x", "-Dmaven.repo.local=D:\x", "/D:C:\x"
_SWITCH_VALUE = re.compile(r"^[/-][^=:]+[=:](.+)$")
_NESTED_QUOTE = re.compile(r'""(?=[^"\s])')
# "/S", "/quiet", "/X{GUID}": a switch, never a location.
_BARE_SWITCH = re.compile(r"^/[^/\\]*$")


def expand_environment(text: str, environment: Mapping[str, str] | None = None) -> str:
    """Expand ``%NAME%`` references case-insensitively; unknown names stay as written."""

    values = {key.casefold(): value for key, value in (environment or os.environ).items()}
    return _VARIABLE.sub(lambda match: values.get(match.group(1).casefold(), match.group(0)), text)


def long_path(path: str) -> str:
    """Replace 8.3 short components (``PROGRA~1``) with their long names.

    Windows only, and only when a component can be short (it contains ``~``).
    When the full path no longer exists, its longest existing parent is
    expanded and the rest is kept as written.
    """

    if os.name != "nt" or "~" not in path:
        return path
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_long_path_name = kernel32.GetLongPathNameW
    get_long_path_name.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_long_path_name.restype = wintypes.DWORD
    size = 512
    for _attempt in range(3):
        buffer = ctypes.create_unicode_buffer(size)
        length = int(get_long_path_name(path, buffer, size))
        if length == 0:
            parent, name = os.path.split(path)
            if not name or parent == path:
                return path
            return os.path.join(long_path(parent), name)
        if length < size:
            return buffer.value
        size = length + 1
    return path


def normalize_path(
    text: str,
    environment: Mapping[str, str] | None = None,
    *,
    pathmod: ModuleType = os.path,
) -> str | None:
    """Return the absolute, long-name, case-folded form, or ``None`` if not absolute."""

    value = expand_environment(text.strip().strip('"').strip(), environment)
    if value.startswith(_EXTENDED_UNC):
        value = "\\\\" + value[len(_EXTENDED_UNC) :]
    elif value.startswith(_EXTENDED):
        value = value[len(_EXTENDED) :]
    if not value or not pathmod.isabs(value):
        return None
    return str(pathmod.normcase(pathmod.normpath(long_path(pathmod.normpath(value)))))


def command_paths(
    command: str,
    environment: Mapping[str, str] | None = None,
    *,
    pathmod: ModuleType = os.path,
) -> tuple[str, ...]:
    """Return the normalised absolute paths a command line names, program first.

    That is the program itself (quoted, or unquoted even when it contains
    spaces), then quoted arguments, unquoted absolute arguments and the values
    of ``name=value`` and ``/switch:value`` arguments. Icon indexes and rundll32
    entry points are dropped. Relative names such as ``MsiExec.exe`` name no
    location and are skipped.
    """

    text = expand_environment(command, environment).strip()
    found: list[str] = []

    def add(raw: str) -> None:
        value = raw.strip().strip("\"',;")
        entry = _TRAILING_ENTRY.match(value)
        if entry is not None:
            value = entry.group(1)
        normalized = normalize_path(value, environment, pathmod=pathmod)
        if normalized is not None:
            found.append(normalized)

    rest = text
    if text and not text.startswith('"'):
        program = _leading_program(text)
        named = len(found)
        add(program)
        if len(found) > named:  # an absolute program; "KEY=VALUE" is read below
            rest = text[len(program) :]
    # cmd.exe /c ""C:\Some Dir\x.bat" /S" wraps the whole command in one more pair.
    rest = _NESTED_QUOTE.sub('"', rest)
    for match in _QUOTED.finditer(rest):
        add(match.group(1))
    for token in _QUOTED.sub(" ", rest).split():
        switch = _SWITCH_VALUE.match(token)
        if switch is not None:
            add(switch.group(1))
        elif "=" in token:
            add(token.split("=", 1)[1])
        elif not _BARE_SWITCH.match(token):
            add(token)
    return tuple(dict.fromkeys(found))


def _leading_program(text: str) -> str:
    """The program of an unquoted command, which may itself contain spaces.

    Like Windows, try ever longer prefixes ending at a space: the first one that
    ends in a program extension or names an existing file wins.
    """

    ends = [match.start() for match in re.finditer(r"\s", text)] + [len(text)]
    for end in ends:
        candidate = text[:end]
        if candidate.casefold().endswith(_PROGRAM_SUFFIXES) or os.path.isfile(candidate):
            return candidate
    return text[: ends[0]]


def program_and_paths(
    command: str,
    environment: Mapping[str, str] | None = None,
    *,
    usable: Callable[[str], bool],
) -> tuple[str | None, tuple[str, ...], tuple[str, ...]]:
    """Split a command's usable paths into its program, folders and other files.

    The program is the first usable file the command names (for rundll32 that
    is the DLL, since rundll32 itself belongs to Windows). Arguments that are
    existing folders are folders; other file arguments, such as a log file,
    are returned as files only, so they never claim their whole parent folder.
    """

    program: str | None = None
    folders: list[str] = []
    files: list[str] = []
    for path in command_paths(command, environment):
        if not usable(path):
            continue
        if os.path.isdir(path):
            folders.append(path)
        elif program is None:
            program = path
        else:
            files.append(path)
    return program, tuple(folders), tuple(files)


__all__ = [
    "command_paths",
    "expand_environment",
    "long_path",
    "normalize_path",
    "program_and_paths",
]
