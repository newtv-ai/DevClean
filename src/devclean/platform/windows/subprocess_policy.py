"""Windows GUI child-process policy.

The packaged DevClean executable uses the Windows GUI subsystem.  Console tools
started from that process would otherwise be allowed to allocate a visible
console window, which produces the distracting cmd/PowerShell flashes users see
while application roots are being discovered.

This module is intentionally opt-in.  The CLI and tests keep normal subprocess
semantics; only the GUI entry point installs the policy.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

_INSTALLED = False
_ORIGINAL_POPEN = subprocess.Popen


def _hidden_console_creationflags(creationflags: int) -> int:
    """Add CREATE_NO_WINDOW unless the caller explicitly requested a console."""

    create_no_window = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    create_new_console = int(getattr(subprocess, "CREATE_NEW_CONSOLE", 0x00000010))
    detached_process = int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    if creationflags & (create_new_console | detached_process):
        return creationflags
    return creationflags | create_no_window


def install_no_console_subprocess_policy() -> None:
    """Prevent console child processes from flashing windows in the GUI build.

    ``CREATE_NO_WINDOW`` changes only console allocation.  GUI applications such
    as Explorer still open normally, and an explicit CREATE_NEW_CONSOLE or
    DETACHED_PROCESS request is respected.
    """

    global _INSTALLED  # noqa: PLW0603 - one process-wide GUI policy is intentional
    if os.name != "nt" or _INSTALLED:
        return

    original = _ORIGINAL_POPEN

    class _NoConsolePopen(original):  # type: ignore[misc,valid-type]
        _devclean_no_console_policy = True

        def __init__(
            self,
            args: Any,
            bufsize: int = -1,
            executable: str | bytes | os.PathLike[str] | os.PathLike[bytes] | None = None,
            stdin: Any = None,
            stdout: Any = None,
            stderr: Any = None,
            preexec_fn: Any = None,
            close_fds: bool = True,
            shell: bool = False,
            cwd: str | bytes | os.PathLike[str] | os.PathLike[bytes] | None = None,
            env: Any = None,
            universal_newlines: bool | None = None,
            startupinfo: Any = None,
            creationflags: int = 0,
            restore_signals: bool = True,
            start_new_session: bool = False,
            pass_fds: tuple[int, ...] = (),
            *,
            user: str | int | None = None,
            group: str | int | None = None,
            extra_groups: Any = None,
            encoding: str | None = None,
            errors: str | None = None,
            text: bool | None = None,
            umask: int = -1,
            pipesize: int = -1,
            process_group: int | None = None,
        ) -> None:
            super().__init__(
                args,
                bufsize,
                executable,
                stdin,
                stdout,
                stderr,
                preexec_fn,
                close_fds,
                shell,
                cwd,
                env,
                universal_newlines,
                startupinfo,
                _hidden_console_creationflags(creationflags),
                restore_signals,
                start_new_session,
                pass_fds,
                user=user,
                group=group,
                extra_groups=extra_groups,
                encoding=encoding,
                errors=errors,
                text=text,
                umask=umask,
                pipesize=pipesize,
                process_group=process_group,
            )

    subprocess.Popen = _NoConsolePopen  # type: ignore[assignment]
    _INSTALLED = True


__all__ = ["install_no_console_subprocess_policy"]
