"""Windows GUI child-process policy.

The packaged DevClean executable uses the Windows GUI subsystem. Console tools
started from that process would otherwise be allowed to allocate a visible
console window, producing the distracting cmd/PowerShell flashes seen while
source-audited application roots are discovered.

The same policy starts every program by absolute path. Windows would otherwise
look for a bare program name next to DevClean.exe and in the current directory
first -- usually Downloads -- and a hidden console would make a planted program
invisible (see ``devclean.platform.windows.executables``).

This module is intentionally opt-in. The CLI and tests keep normal subprocess
semantics; only the GUI entry point installs the policy.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from devclean.platform.windows.executables import resolve_command

_INSTALLED = False
_EXECUTABLE_POSITION = 2
_SHELL_POSITION = 8
_CREATIONFLAGS_POSITION = 13


def _hidden_console_creationflags(creationflags: int) -> int:
    """Add CREATE_NO_WINDOW unless the caller explicitly requested a console."""

    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    create_new_console = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010))
    detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    if creationflags & (create_new_console | detached_process):
        return creationflags
    return creationflags | create_no_window


def _absolute_program_arguments(positional: list[Any], kwargs: dict[str, Any]) -> None:
    """Replace a bare or relative program name with its absolute path in place."""

    executable = (
        positional[_EXECUTABLE_POSITION]
        if len(positional) > _EXECUTABLE_POSITION
        else kwargs.get("executable")
    )
    shell = (
        positional[_SHELL_POSITION] if len(positional) > _SHELL_POSITION else kwargs.get("shell")
    )
    if executable is not None or shell:
        return
    command = positional[0] if positional else kwargs.get("args")
    if not isinstance(command, (list, tuple)):
        # DevClean only starts programs from argument lists; a command string
        # would be split by Windows itself and bypass this check.
        raise ValueError("GUI child processes must be started from an argument list")
    resolved = resolve_command(command)
    if positional:
        positional[0] = resolved
    else:
        kwargs["args"] = resolved


class _NoConsolePopen(subprocess.Popen[Any]):
    """Popen variant that hides console windows and starts programs by absolute path."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        positional = list(args)
        _absolute_program_arguments(positional, kwargs)
        if len(positional) > _CREATIONFLAGS_POSITION:
            creationflags = int(positional[_CREATIONFLAGS_POSITION])
            positional[_CREATIONFLAGS_POSITION] = _hidden_console_creationflags(creationflags)
        else:
            creationflags = int(kwargs.get("creationflags", 0))
            kwargs["creationflags"] = _hidden_console_creationflags(creationflags)
        super().__init__(*positional, **kwargs)


def install_no_console_subprocess_policy() -> None:
    """Prevent console child processes from flashing windows in the GUI build.

    ``CREATE_NO_WINDOW`` changes only console allocation. GUI applications such
    as Explorer still open normally, and an explicit CREATE_NEW_CONSOLE or
    DETACHED_PROCESS request is respected. Replacing ``Popen`` with a subclass,
    rather than a wrapper function, also preserves runtime uses such as
    ``subprocess.Popen[str]`` in type aliases imported by the packaged GUI.
    """

    global _INSTALLED
    if os.name != "nt" or _INSTALLED:
        return
    subprocess.__dict__["Popen"] = _NoConsolePopen
    _INSTALLED = True


__all__ = ["install_no_console_subprocess_policy"]
