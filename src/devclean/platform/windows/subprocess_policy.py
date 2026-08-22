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
_ORIGINAL_POPEN = subprocess.Popen
_CREATIONFLAGS_POSITION = 13


def _hidden_console_creationflags(creationflags: int) -> int:
    """Add CREATE_NO_WINDOW unless the caller explicitly requested a console."""

    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    create_new_console = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010))
    detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    if creationflags & (create_new_console | detached_process):
        return creationflags
    return creationflags | create_no_window


def _no_console_popen(*args: Any, **kwargs: Any) -> Any:
    """Call the real Popen after applying the GUI console-allocation policy."""

    positional = list(args)
    if len(positional) > _CREATIONFLAGS_POSITION:
        creationflags = int(positional[_CREATIONFLAGS_POSITION])
        positional[_CREATIONFLAGS_POSITION] = _hidden_console_creationflags(creationflags)
    else:
        creationflags = int(kwargs.get("creationflags", 0))
        kwargs["creationflags"] = _hidden_console_creationflags(creationflags)
    return _ORIGINAL_POPEN(*positional, **kwargs)


def install_no_console_subprocess_policy() -> None:
    """Prevent console child processes from flashing windows in the GUI build.

    ``CREATE_NO_WINDOW`` changes only console allocation. GUI applications such
    as Explorer still open normally, and an explicit CREATE_NEW_CONSOLE or
    DETACHED_PROCESS request is respected.
    """

    global _INSTALLED
    if os.name != "nt" or _INSTALLED:
        return
    setattr(subprocess, "Popen", _no_console_popen)
    _INSTALLED = True


__all__ = ["install_no_console_subprocess_policy"]
