"""Windows GUI child-process policy.

The packaged DevClean executable uses the Windows GUI subsystem. Console tools
started from that process would otherwise be allowed to allocate a visible
console window, producing the distracting cmd/PowerShell flashes seen while
source-audited application roots are discovered.

This module is intentionally opt-in. The CLI and tests keep normal subprocess
semantics; only the GUI entry point installs the policy.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

_INSTALLED = False
_CREATIONFLAGS_POSITION = 13


def _hidden_console_creationflags(creationflags: int) -> int:
    """Add CREATE_NO_WINDOW unless the caller explicitly requested a console."""

    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    create_new_console = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010))
    detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    if creationflags & (create_new_console | detached_process):
        return creationflags
    return creationflags | create_no_window


class _NoConsolePopen(subprocess.Popen[Any]):
    """Popen variant that preserves the class API while hiding console windows."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        positional = list(args)
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
